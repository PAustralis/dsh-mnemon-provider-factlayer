"""/health 报告：派生水位、备份状态、生效配置快照、各路最近空结果率与向量降级计数。"""

from __future__ import annotations

from .db import Database
from .derive import STAGES, DerivePipeline
from .schema import get_meta

LAST_BACKUP_AT_KEY = "last_backup_at"


def last_backup_at(db: Database) -> int | None:
    raw = get_meta(db, LAST_BACKUP_AT_KEY)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def health_report(db: Database, pipeline: DerivePipeline, backup_scheduler=None, retrieval=None) -> dict:
    """备份状态来自调度器；没有调度器（测试/只读用法）时报告「无失败」。

    检索层接入后（``retrieval`` 非 None）额外暴露：生效配置快照（模型名/维度/
    启用路线/开关）、各路最近空结果率、向量降级/错误计数、自动降级状态。
    """
    run_state = (
        backup_scheduler.status()
        if backup_scheduler is not None
        else {"last_backup_error": None, "backup_consecutive_failures": 0}
    )
    report = {
        "pending": {stage: pipeline.pending_count(stage) for stage in STAGES},
        "failures": {stage: pipeline.failures[stage] for stage in STAGES},
        "last_backup_at": last_backup_at(db),
        "last_backup_error": run_state["last_backup_error"],
        "backup_consecutive_failures": run_state["backup_consecutive_failures"],
    }
    if retrieval is not None:
        snapshot = retrieval.cfg.snapshot()
        snapshot["effective_routes"] = list(retrieval.routes)
        stats = retrieval.stats.snapshot()
        stats["degraded"] = bool(retrieval.degrade.get("degraded"))
        stats["degrade_reason"] = retrieval.degrade.get("reason")
        report["config"] = snapshot
        report["retrieval"] = stats
    return report
