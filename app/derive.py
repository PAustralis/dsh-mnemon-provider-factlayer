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
import os
import threading
import time
from typing import Callable

from . import textnorm
from .db import Database
from .index_text import INDEX_TEXT_VERSION, INDEX_TEXT_VERSION_KEY, index_text_for
from .embeddings import (
    CircuitBreaker,
    EmbeddingProvider,
    VectorIndex,
    redact_error,
    store_embedding,
    write_embedding_rows,
)
from .write_path import now_ms

logger = logging.getLogger("factlayer.derive")

STAGES = ("fts", "embedding", "entity", "graph")
BATCH_LIMIT = 50
DEFAULT_BATCH_SIZE = 500

# embedding 失败退避：1s / 2s / 4s … 上限 60s（连续第 3 次失败会先触发熔断）。
EMBEDDING_BACKOFF_BASE_SECONDS = 1.0
EMBEDDING_BACKOFF_MAX_SECONDS = 60.0

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
        # 索引文本 = 日期前缀 + content（docs/02 v9）；content 本身不入派生表。
        indexed = index_text_for(memory)
        ngrams = textnorm.ngram_text(indexed)
        trigram_text = textnorm.normalize(indexed)
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
        # embedding 文本同样带日期前缀：年份只存在于日期字段时，语义路也能命中。
        vectors = provider.embed([index_text_for(memory)])
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
    @staticmethod
    def _resolve_batch_size(batch_size: int | None) -> int:
        """批大小：显式参数 > ``DSH_DERIVE_BATCH_SIZE`` > 500；非法值回退默认。"""
        if batch_size is None:
            raw = os.environ.get("DSH_DERIVE_BATCH_SIZE")
            try:
                batch_size = int(raw) if raw not in (None, "") else DEFAULT_BATCH_SIZE
            except ValueError:
                logger.warning(
                    "DSH_DERIVE_BATCH_SIZE=%r 不是整数，回退默认 %d",
                    raw,
                    DEFAULT_BATCH_SIZE,
                )
                batch_size = DEFAULT_BATCH_SIZE
        return max(1, int(batch_size))

    def __init__(
        self,
        db: Database,
        stub_success: bool = True,
        *,
        embedding_provider: EmbeddingProvider | None = None,
        embedding_dim: int | None = None,
        fts_enabled: bool = True,
        vector_index: VectorIndex | None = None,
        batch_size: int | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.db = db
        self.vector_index = vector_index
        self.embedding_provider = embedding_provider
        self.embedding_dim = embedding_dim
        self.batch_size = self._resolve_batch_size(batch_size)
        self.failures: dict[str, int] = {stage: 0 for stage in STAGES}
        # 最近一次 embedding 派生失败的**脱敏**错误（/health 可读，不含 key）
        self.last_embedding_error: str | None = None
        self._clock = clock or time.monotonic
        # 与检索层向量路共用熔断实现：连续 3 次失败 → 5 分钟（见 embeddings）。
        self.embedding_circuit = CircuitBreaker(label="embedding provider", clock=self._clock)
        self.embedding_circuit_skips = 0
        self._embedding_backoff_until = 0.0
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
            if stage == "embedding":
                processed[stage] = self._run_embedding_stage()
            else:
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

    def _run_embedding_stage(self) -> int:
        """embedding 派生：熔断/退避检查 → 失败计数 → 成功写水位。

        失败**不写** derive_state（保持 pending 等重试）；连续失败按指数退避
        （1s/2s/4s…上限 60s），连续 3 次失败由共享熔断器打开 5 分钟。熔断期间
        每轮记一次 ``embedding_circuit_skips``，避免静默空转。
        """
        if not self.embedding_circuit.allow():
            self._skip_embedding_due_to_circuit()
            return 0
        with self._lock:
            backoff_remaining = self._embedding_backoff_until - self._clock()
        if backoff_remaining > 0:
            logger.info(
                "embedding 退避中（剩余 %.1fs），本轮跳过（pending %d 条保持）",
                backoff_remaining,
                self.pending_count("embedding"),
            )
            return 0

        count = 0
        for memory in self.pending("embedding"):
            # 同批内触发熔断/退避后立即停手，避免继续打 API 烧钱。
            if not self.embedding_circuit.allow():
                self._skip_embedding_due_to_circuit()
                break
            with self._lock:
                if self._clock() < self._embedding_backoff_until:
                    break
            try:
                self.processors["embedding"](memory)
            except Exception as exc:
                self._record_embedding_failure(exc, memory_id=memory["id"])
                break  # 退避后再试；失败不写 done
            self._record_embedding_success()
            with self.db.write_txn() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO derive_state(memory_id, stage, done_at) "
                    "VALUES (?,?,?)",
                    (memory["id"], "embedding", now_ms()),
                )
            count += 1
        return count

    def _skip_embedding_due_to_circuit(self) -> None:
        with self._lock:
            self.embedding_circuit_skips += 1
        state = self.embedding_circuit.state()
        logger.warning(
            "embedding 熔断中（剩余 %.0fs，连续失败 %d 次），本轮跳过 embedding"
            "（pending %d 条保持）",
            state["open_seconds_remaining"],
            state["consecutive_failures"],
            self.pending_count("embedding"),
        )

    def _record_embedding_failure(
        self, exc: BaseException, *, memory_id: str | None = None
    ) -> None:
        """失败计数 + 脱敏 last_error + 指数退避 + 共享熔断。"""
        self.embedding_circuit.record_failure(exc)
        message = redact_error(exc)
        state = self.embedding_circuit.state()
        consecutive = max(1, int(state["consecutive_failures"]))
        backoff = min(
            EMBEDDING_BACKOFF_BASE_SECONDS * (2 ** (consecutive - 1)),
            EMBEDDING_BACKOFF_MAX_SECONDS,
        )
        with self._lock:
            self.failures["embedding"] += 1
            failures = self.failures["embedding"]
            self.last_embedding_error = message
            self._embedding_backoff_until = self._clock() + backoff
        logger.exception(
            "embedding processor failed for memory %s（失败计数 %d，连续 %d 次，"
            "退避 %.1fs，条目保持未完成）：%s",
            memory_id or "-",
            failures,
            consecutive,
            backoff,
            message,
        )

    def _record_embedding_success(self) -> None:
        self.embedding_circuit.record_success()
        with self._lock:
            self._embedding_backoff_until = 0.0
            self.last_embedding_error = None

    def embedding_state(self) -> dict:
        """embedding 派生健康（/health）：熔断、退避、跳过次数、脱敏错误。"""
        circuit = self.embedding_circuit.state()
        with self._lock:
            backoff = max(0.0, self._embedding_backoff_until - self._clock())
            failures = self.failures["embedding"]
            skips = self.embedding_circuit_skips
            last_error = self.last_embedding_error
        return {
            "configured": self.embedding_provider is not None,
            "failures": failures,
            "circuit": circuit,
            "circuit_skips": skips,
            "backoff_remaining_seconds": round(backoff, 3),
            "last_error": last_error,
        }

    # -- 全量重建 ---------------------------------------------------------------
    def rebuild_all(
        self,
        *,
        skip_embedding: bool = False,
        allow_embedding_failure: bool = False,
    ) -> dict[str, int]:
        """全量重建：**一个写事务内**清空派生层并重算全部条目。

        - 事实层 memories 永不动。
        - embedding 在事务外先算好（避免持写锁做网络调用），写库与其余派生在
          同一事务内完成，查询永远看到完整旧/新索引。
        - ``skip_embedding``：跳过向量派生——不清空 ``embeddings``、不写 embedding
          水位（迁移启动时无 provider 的降级路径，避免把旧向量一起删掉）。
        - ``allow_embedding_failure``：provider 故障时 ERROR 日志 + 降级为仅重建
          ngram/FTS，而不是让整次重建失败（旧向量保留，待后续 tick/rebuild 补算）。
        - 成功后清空内存向量矩阵，下次检索全量载入（与 gseq 对齐）。
        """
        self.last_embedding_error = None
        rows = [
            dict(row)
            for row in self.db.read_conn.execute(
                "SELECT * FROM memories ORDER BY gseq"
            ).fetchall()
        ]
        # skip_embedding 时旧向量不会被重算；若库内已有向量，就不能宣称索引文本版本
        # 已升级（否则旧 content-only 向量会以 v2 名义继续被向量路使用，静默错配）。
        has_embeddings = bool(
            self.db.read_conn.execute("SELECT 1 FROM embeddings LIMIT 1").fetchone()
        )
        claim_index_version = (not skip_embedding) or (not has_embeddings)
        vectors: dict[str, list[float]] = {}
        if not skip_embedding and self.embedding_provider is not None:
            try:
                for memory in rows:
                    result = self.embedding_provider.embed([index_text_for(memory)])
                    if not result:
                        raise RuntimeError("embedding provider 返回空结果")
                    vectors[memory["id"]] = result[0]
            except Exception as exc:
                # 全量重建的 embedding 失败同样计数/退避/熔断，绝不只打日志。
                self._record_embedding_failure(exc, memory_id="rebuild")
                if not allow_embedding_failure:
                    raise
                skip_embedding = True
                logger.error(
                    "全量重建：embedding 派生失败，降级为仅重建 ngram/FTS"
                    "（旧向量数据保留，待后续 tick / rebuild 补算）：%s",
                    self.last_embedding_error,
                )

        counts = {stage: 0 for stage in STAGES}
        # 先在事务外把三类派生行的参数算好，再在写事务里按 batch_size 用
        # executemany 批量写入（避免逐条 INSERT 的 Python/SQLite 往返）。
        done_at = now_ms()
        ngram_rows: list[tuple[str, str]] = []
        bigram_rows: list[tuple[int, str]] = []
        trigram_rows: list[tuple[int, str]] = []
        embedding_rows: list[tuple[str, list[float]]] = []
        derive_rows_by_memory: list[list[tuple[str, str, int]]] = []
        for memory in rows:
            memory_id = memory["id"]
            rowid = int(memory["gseq"])
            indexed = index_text_for(memory)
            ngrams = textnorm.ngram_text(indexed)
            trigram_text = textnorm.normalize(indexed)
            ngram_rows.append((memory_id, ngrams))
            bigram_rows.append((rowid, ngrams))
            trigram_rows.append((rowid, trigram_text))
            counts["fts"] += 1
            if not skip_embedding:
                if self.embedding_provider is not None:
                    embedding_rows.append((memory_id, vectors[memory_id]))
                counts["embedding"] += 1  # stub 语义：无 provider 视为完成
            counts["entity"] += 1
            counts["graph"] += 1
            stages_done: list[tuple[str, str, int]] = []
            for stage in STAGES:
                if skip_embedding and stage == "embedding":
                    continue  # 未派生：留空水位，/health 可见、后续 tick 可重试
                stages_done.append((memory_id, stage, done_at))
            derive_rows_by_memory.append(stages_done)

        batches = 0
        with self.db.write_txn() as conn:
            conn.execute("DELETE FROM derive_state")
            conn.execute("DELETE FROM entities")
            conn.execute("DELETE FROM memories_ngrams")
            conn.execute("DELETE FROM memories_fts_bigram")
            conn.execute("DELETE FROM memories_fts_trigram")
            if not skip_embedding:
                conn.execute("DELETE FROM embeddings")
                # 维度/模型指纹也是派生元数据：重建后必须重新推断
                conn.execute(
                    "DELETE FROM meta WHERE key IN ('embedding_model','embedding_dim')"
                )
            total = len(rows)
            for start in range(0, total, self.batch_size):
                end = min(start + self.batch_size, total)
                batches += 1
                self.db.executemany(
                    conn,
                    "INSERT INTO memories_ngrams(memory_id, ngram_text) VALUES (?, ?)",
                    ngram_rows[start:end],
                )
                self.db.executemany(
                    conn,
                    "INSERT INTO memories_fts_bigram(rowid, ngram_text) VALUES (?, ?)",
                    bigram_rows[start:end],
                )
                self.db.executemany(
                    conn,
                    "INSERT INTO memories_fts_trigram(rowid, content) VALUES (?, ?)",
                    trigram_rows[start:end],
                )
                if not skip_embedding and self.embedding_provider is not None:
                    write_embedding_rows(
                        conn,
                        embedding_rows[start:end],
                        model_id=self.embedding_provider.model_id,
                        configured_dim=self.embedding_dim,
                        executemany=self.db.executemany,
                    )
                derive_batch = [
                    row
                    for group in derive_rows_by_memory[start:end]
                    for row in group
                ]
                self.db.executemany(
                    conn,
                    "INSERT OR REPLACE INTO derive_state(memory_id, stage, done_at) "
                    "VALUES (?,?,?)",
                    derive_batch,
                )
            # 版本号与派生结果同一事务落库：重建成功才宣称索引文本已升级；
            # 中途失败回滚 → 版本保持旧值，下次启动会重试。
            # skip_embedding 且库内已有旧向量时**不写版本**：ngram 是新版、向量还是
            # 旧版，此时宣称 v2 会让旧向量冒充新索引（静默错配）。
            if claim_index_version:
                conn.executemany(
                    "INSERT INTO meta(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    [(INDEX_TEXT_VERSION_KEY, str(INDEX_TEXT_VERSION))],
                )
            else:
                logger.warning(
                    "rebuild 跳过 embedding 且库内已有旧向量：不写 index_text_version"
                    "（版本保持旧值，待有 provider 时重新重建向量）"
                )
        logger.info(
            "rebuild 批量写入：rows=%d batch_size=%d batches=%d "
            "(ngrams/fts/embeddings/derive_state 均 executemany)",
            total,
            self.batch_size,
            batches,
        )
        if self.vector_index is not None:
            self.vector_index.invalidate()
        if not skip_embedding:
            self._record_embedding_success()
        with self._lock:
            if skip_embedding:
                # 向量未派生：保留 embedding 失败计数（/health 告警），其余清零。
                for stage in ("fts", "entity", "graph"):
                    self.failures[stage] = 0
            else:
                self.failures = {stage: 0 for stage in STAGES}
        return counts

    def rebuild_after_migration(self) -> dict:
        """旧库迁移/派生索引缺失后的**启动强制重建**（v8 审阅 P0）。

        ``init_db()`` 检测到 migration 或索引行数 < 事实行数时调用：

        - ngram + FTS 必须重建，否则旧记忆永远搜不到（默认派生循环关闭、
          ``/derive/tick`` 每轮只处理 50 条，不能当补数手段）。
        - 未配置 embedding provider：跳过向量派生并记录原因（warning + /health），
          不报错；旧 ``embeddings`` 行原样保留。
        - provider 故障：ERROR 日志后降级为仅重建 ngram/FTS（旧向量保留）。
        """
        skipped_reason: str | None = None
        if self.embedding_provider is None:
            skipped_reason = (
                "未配置 embedding provider（EMBEDDING_MODEL 为空）："
                "跳过向量派生，仅重建 ngram/FTS 索引"
            )
            logger.warning("迁移后派生重建：%s", skipped_reason)
        counts = self.rebuild_all(
            skip_embedding=skipped_reason is not None,
            allow_embedding_failure=True,
        )
        return {
            "counts": counts,
            "embedding_skipped": skipped_reason,
            "embedding_error": self.last_embedding_error,
        }
