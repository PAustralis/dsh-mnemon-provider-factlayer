"""derive_state 水位循环。

stage ∈ {fts, embedding, entity, graph}：
- ``fts``：生成 ``memories_ngrams``（单字+双字）并写 ``memories_fts_bigram`` /
  ``memories_fts_trigram``（R2/R3 的索引；派生层，可弃可重建）。
- ``embedding``：接了真实 provider 时算向量（L2 归一化 + model_id/dim 指纹）；
  没接 provider 时保持第一层的 stub 语义（``DSH_DERIVE_STUB_SUCCESS`` 控制成功/失败）。
- ``entity`` / ``graph``：第一层 stub；R4 图邻居本层未接（见 retrieval.GraphRoute）。

循环 = 找未完成条目（ORDER BY recorded_at LIMIT 50）→ 逐条 processor →
成功写 derive_state(done_at)，失败计失败次数不写 done（下轮重试，绝不静默跳过）。
全量重建 = 清空派生层，循环自动补齐；事实层（memories）永不动。
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

from . import textnorm
from .db import Database
from .embeddings import EmbeddingProvider, store_embedding
from .write_path import now_ms

logger = logging.getLogger("factlayer.derive")

STAGES = ("fts", "embedding", "entity", "graph")
BATCH_LIMIT = 50

Processor = Callable[[dict], None]  # 接收 memory 行 dict；抛异常 = 该条本阶段失败


def _make_stub(stage: str, stub_success: bool) -> Processor:
    def stub(memory: dict) -> None:
        if not stub_success:
            raise RuntimeError(f"{stage} stub 被配置为失败（DSH_DERIVE_STUB_SUCCESS=0）")
        logger.info("%s stub done for memory %s", stage, memory["id"])

    return stub


def make_fts_processor(db: Database) -> Processor:
    """stage=fts：写 ngrams + 两张 FTS5 虚表（重复执行幂等，可安全重建）。"""

    def process(memory: dict) -> None:
        content = memory["content"]
        ngrams = textnorm.ngram_text(content)
        trigram_text = textnorm.normalize(content)
        with db.write_txn() as conn:
            row = conn.execute(
                "SELECT rowid FROM memories WHERE id=?", (memory["id"],)
            ).fetchone()
            if row is None:
                raise RuntimeError(f"memory {memory['id']} 不存在，fts 派生中止")
            rowid = int(row[0])
            conn.execute(
                "INSERT OR REPLACE INTO memories_ngrams(memory_id, ngram_text) "
                "VALUES (?, ?)",
                (memory["id"], ngrams),
            )
            # 独立（非 external-content）FTS5 表：先删同 rowid 再插，保证幂等
            conn.execute("DELETE FROM memories_fts_bigram WHERE rowid=?", (rowid,))
            conn.execute(
                "INSERT INTO memories_fts_bigram(rowid, ngram_text) VALUES (?, ?)",
                (rowid, ngrams),
            )
            conn.execute("DELETE FROM memories_fts_trigram WHERE rowid=?", (rowid,))
            conn.execute(
                "INSERT INTO memories_fts_trigram(rowid, content) VALUES (?, ?)",
                (rowid, trigram_text),
            )

    return process


def make_embedding_processor(
    db: Database,
    provider: EmbeddingProvider,
    configured_dim: int | None,
) -> Processor:
    """stage=embedding：调 provider 算向量并入库（写库前 L2 归一化）。"""

    def process(memory: dict) -> None:
        vectors = provider.embed([memory["content"]])
        if not vectors:
            raise RuntimeError("embedding provider 返回空结果")
        store_embedding(
            db,
            memory["id"],
            vectors[0],
            model_id=provider.model_id,
            configured_dim=configured_dim,
        )

    return process


class DerivePipeline:
    def __init__(
        self,
        db: Database,
        stub_success: bool = True,
        *,
        embedding_provider: EmbeddingProvider | None = None,
        embedding_dim: int | None = None,
        fts_enabled: bool = True,
    ) -> None:
        self.db = db
        self.failures: dict[str, int] = {stage: 0 for stage in STAGES}
        self._lock = threading.Lock()
        processors: dict[str, Processor] = {
            stage: _make_stub(stage, stub_success)
            for stage in ("embedding", "entity", "graph")
        }
        if fts_enabled:
            processors["fts"] = make_fts_processor(db)
        else:
            processors["fts"] = _make_stub("fts", stub_success)
        if embedding_provider is not None:
            processors["embedding"] = make_embedding_processor(
                db, embedding_provider, embedding_dim
            )
        self.processors: dict[str, Processor] = {
            stage: processors[stage] for stage in STAGES
        }

    # -- 注册/替换 processor（后续层替换 stub 用） -----------------------------
    def register(self, stage: str, processor: Processor) -> None:
        if stage not in STAGES:
            raise ValueError(f"unknown stage: {stage}")
        self.processors[stage] = processor

    # -- 查询 ------------------------------------------------------------------
    def pending(self, stage: str, limit: int = BATCH_LIMIT) -> list[dict]:
        rows = self.db.read_conn.execute(
            "SELECT m.*, m.rowid AS _rowid FROM memories m "
            "LEFT JOIN derive_state d ON d.memory_id = m.id AND d.stage = ? "
            "WHERE d.memory_id IS NULL ORDER BY m.recorded_at LIMIT ?",
            (stage, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def pending_count(self, stage: str) -> int:
        return self.db.read_conn.execute(
            "SELECT COUNT(*) FROM memories m "
            "LEFT JOIN derive_state d ON d.memory_id = m.id AND d.stage = ? "
            "WHERE d.memory_id IS NULL",
            (stage,),
        ).fetchone()[0]

    # -- 循环 ------------------------------------------------------------------
    def tick(self) -> dict[str, int]:
        """跑一轮：每个 stage 处理最多 50 条未完成条目。返回 {stage: 处理条数}。"""
        processed: dict[str, int] = {}
        for stage in STAGES:
            processed[stage] = self._run_stage(stage)
        return processed

    def _run_stage(self, stage: str) -> int:
        count = 0
        failures = 0
        for memory in self.pending(stage):
            try:
                self.processors[stage](memory)
            except Exception:
                with self._lock:
                    self.failures[stage] += 1
                    failures = self.failures[stage]
                logger.exception(
                    "%s processor failed for memory %s（失败计数 %d，条目保持未完成）",
                    stage,
                    memory["id"],
                    failures,
                )
                continue  # 失败不写 done，下轮重试；绝不静默跳过
            with self.db.write_txn() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO derive_state(memory_id, stage, done_at) "
                    "VALUES (?,?,?)",
                    (memory["id"], stage, now_ms()),
                )
            count += 1
        return count

    # -- 全量重建 ---------------------------------------------------------------
    def rebuild_all(self) -> None:
        """全量重建 = 清空派生层（含 ngrams / 两张 FTS5），循环自动补齐。

        事实层 memories 永不动；derive_state 清空后 fts 与 embedding stage 会重跑。
        """
        with self.db.write_txn() as conn:
            conn.execute("DELETE FROM derive_state")
            conn.execute("DELETE FROM embeddings")
            conn.execute("DELETE FROM entities")
            conn.execute("DELETE FROM memories_ngrams")
            conn.execute("DELETE FROM memories_fts_bigram")
            conn.execute("DELETE FROM memories_fts_trigram")
            # 维度/模型指纹也是派生元数据：重建后必须重新推断，否则新模型会被旧指纹挡住
            conn.execute("DELETE FROM meta WHERE key IN ('embedding_model','embedding_dim')")
        # TODO（后续层）: 清空 FalkorDB 图（本层不连接 FalkorDB）
        with self._lock:
            self.failures = {stage: 0 for stage in STAGES}
