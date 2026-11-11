"""derive_state 水位循环骨架。

stage ∈ {embedding, entity, graph}；循环 = 找未完成条目（ORDER BY recorded_at
LIMIT 50）→ 逐条 processor → 成功写 derive_state(done_at)，失败计失败次数不写 done。
本层 processor 全部是 stub：记录到日志即视为成功（可用 DSH_DERIVE_STUB_SUCCESS=0
让 embedding stub 失败以验证失败计数）。真实 embedding/实体抽取/FalkorDB 同步留待后续层。
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

from .db import Database
from .write_path import now_ms

logger = logging.getLogger("factlayer.derive")

STAGES = ("embedding", "entity", "graph")
BATCH_LIMIT = 50

Processor = Callable[[dict], None]  # 接收 memory 行 dict；抛异常 = 该条本阶段失败


def _make_stub(stage: str, stub_success: bool) -> Processor:
    def stub(memory: dict) -> None:
        if not stub_success:
            raise RuntimeError(f"{stage} stub 被配置为失败（DSH_DERIVE_STUB_SUCCESS=0）")
        logger.info("%s stub done for memory %s", stage, memory["id"])

    return stub


class DerivePipeline:
    def __init__(self, db: Database, stub_success: bool = True) -> None:
        self.db = db
        self.processors: dict[str, Processor] = {
            stage: _make_stub(stage, stub_success) for stage in STAGES
        }
        self.failures: dict[str, int] = {stage: 0 for stage in STAGES}
        self._lock = threading.Lock()

    # -- 注册/替换 processor（后续层替换 stub 用） -----------------------------
    def register(self, stage: str, processor: Processor) -> None:
        if stage not in STAGES:
            raise ValueError(f"unknown stage: {stage}")
        self.processors[stage] = processor

    # -- 查询 ------------------------------------------------------------------
    def pending(self, stage: str, limit: int = BATCH_LIMIT) -> list[dict]:
        rows = self.db.read_conn.execute(
            "SELECT m.* FROM memories m "
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
        for memory in self.pending(stage):
            try:
                self.processors[stage](memory)
            except Exception:
                with self._lock:
                    self.failures[stage] += 1
                logger.exception(
                    "%s processor failed for memory %s（失败计数 %d，条目保持未完成）",
                    stage,
                    memory["id"],
                    self.failures[stage],
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
        """全量重建 = 清空派生层，循环自动补齐。事实层（memories）永不动。"""
        with self.db.write_txn() as conn:
            conn.execute("DELETE FROM derive_state")
            conn.execute("DELETE FROM embeddings")
            conn.execute("DELETE FROM entities")
        # TODO（后续层）: 清空 FalkorDB 图（本层不连接 FalkorDB）
        with self._lock:
            self.failures = {stage: 0 for stage in STAGES}
