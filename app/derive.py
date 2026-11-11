"""derive_state 水位循环。

stage ∈ {fts, embedding, entity, graph}：
- ``fts``：生成 ``memories_ngrams``（**与基线同构的单混合列**，见 app.textnorm）
  并写 ``memories_fts_bigram`` / ``memories_fts_trigram``（派生层，可弃可重建）。
- ``embedding``：接了真实 provider 时算向量（L2 归一化 + 按行存 model_id/dim）；
  没接 provider 时保持第一层的 stub 语义（``DSH_DERIVE_STUB_SUCCESS`` 控制成功/失败）。
  写事务提交后增量刷新检索层的内存向量矩阵（``vector_index``）。
- ``entity`` / ``graph``：第一层 stub；R4 图邻居本层未接。

循环 = 找未完成条目（ORDER BY gseq LIMIT 50）→ 逐条 processor →
成功写 derive_state(done_at)，失败计失败次数不写 done（下轮重试，绝不静默跳过）。
全量重建 = **一个写事务内**清空派生层并重算全部条目（数据量小）；事实层永不动。
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

from . import textnorm
from .db import Database
from .embeddings import (
    EmbeddingProvider,
    VectorIndex,
    store_embedding,
    write_embedding_row,
)
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
    """stage=fts：写单混合列 ngrams + 两张 FTS5 虚表（重复执行幂等，可安全重建）。"""

    def process(memory: dict) -> None:
        content = memory["content"]
        ngrams = textnorm.ngram_text(content)
        trigram_text = textnorm.normalize(content)
        with db.write_txn() as conn:
            row = conn.execute(
                "SELECT gseq FROM memories WHERE id=?", (memory["id"],)
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
    vector_index: VectorIndex | None = None,
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
            index=vector_index,
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
        vector_index: VectorIndex | None = None,
    ) -> None:
        self.db = db
        self.vector_index = vector_index
        self.embedding_provider = embedding_provider
        self.embedding_dim = embedding_dim
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
                db, embedding_provider, embedding_dim, vector_index
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
            "SELECT m.*, m.gseq AS _gseq FROM memories m "
            "LEFT JOIN derive_state d ON d.memory_id = m.id AND d.stage = ? "
            "WHERE d.memory_id IS NULL ORDER BY m.gseq LIMIT ?",
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
    def rebuild_all(self) -> dict[str, int]:
        """全量重建：**一个写事务内**清空派生层并重算全部条目。

        - 事实层 memories 永不动。
        - embedding 在事务外先算好（避免持写锁做网络调用），写库与其余派生在
          同一事务内完成，查询永远看到完整旧/新索引。
        - 成功后清空内存向量矩阵，下次检索全量载入（与 gseq 对齐）。
        """
        rows = [
            dict(row)
            for row in self.db.read_conn.execute(
                "SELECT * FROM memories ORDER BY gseq"
            ).fetchall()
        ]
        vectors: dict[str, list[float]] = {}
        if self.embedding_provider is not None:
            for memory in rows:
                result = self.embedding_provider.embed([memory["content"]])
                if not result:
                    raise RuntimeError("embedding provider 返回空结果")
                vectors[memory["id"]] = result[0]

        counts = {stage: 0 for stage in STAGES}
        with self.db.write_txn() as conn:
            conn.execute("DELETE FROM derive_state")
            conn.execute("DELETE FROM embeddings")
            conn.execute("DELETE FROM entities")
            conn.execute("DELETE FROM memories_ngrams")
            conn.execute("DELETE FROM memories_fts_bigram")
            conn.execute("DELETE FROM memories_fts_trigram")
            # 维度/模型指纹也是派生元数据：重建后必须重新推断
            conn.execute(
                "DELETE FROM meta WHERE key IN ('embedding_model','embedding_dim')"
            )
            done_at = now_ms()
            for memory in rows:
                memory_id = memory["id"]
                content = memory["content"]
                rowid = int(memory["gseq"])
                ngrams = textnorm.ngram_text(content)
                trigram_text = textnorm.normalize(content)
                conn.execute(
                    "INSERT INTO memories_ngrams(memory_id, ngram_text) VALUES (?, ?)",
                    (memory_id, ngrams),
                )
                conn.execute(
                    "INSERT INTO memories_fts_bigram(rowid, ngram_text) VALUES (?, ?)",
                    (rowid, ngrams),
                )
                conn.execute(
                    "INSERT INTO memories_fts_trigram(rowid, content) VALUES (?, ?)",
                    (rowid, trigram_text),
                )
                counts["fts"] += 1
                if self.embedding_provider is not None:
                    write_embedding_row(
                        conn,
                        memory_id,
                        vectors[memory_id],
                        model_id=self.embedding_provider.model_id,
                        configured_dim=self.embedding_dim,
                    )
                    counts["embedding"] += 1
                else:
                    counts["embedding"] += 1  # stub 语义：无 provider 视为完成
                counts["entity"] += 1
                counts["graph"] += 1
                for stage in STAGES:
                    conn.execute(
                        "INSERT OR REPLACE INTO derive_state(memory_id, stage, done_at) "
                        "VALUES (?,?,?)",
                        (memory_id, stage, done_at),
                    )
        if self.vector_index is not None:
            self.vector_index.invalidate()
        with self._lock:
            self.failures = {stage: 0 for stage in STAGES}
        return counts
