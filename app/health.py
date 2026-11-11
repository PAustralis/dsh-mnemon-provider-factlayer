"""/health 报告：各 stage 未派生条数、失败计数、备份最后成功时间与最近一次失败。"""

from __future__ import annotations

from .db import Database
from .derive import DerivePipeline, STAGES
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


def health_report(db: Database, pipeline: DerivePipeline, backup_scheduler=None) -> dict:
    """备份状态来自调度器；没有调度器（测试/只读用法）时报告「无失败」。"""
    run_state = (
        backup_scheduler.status()
        if backup_scheduler is not None
        else {"last_backup_error": None, "backup_consecutive_failures": 0}
    )
    return {
        "pending": {stage: pipeline.pending_count(stage) for stage in STAGES},
        "failures": {stage: pipeline.failures[stage] for stage in STAGES},
        "last_backup_at": last_backup_at(db),
        "last_backup_error": run_state["last_backup_error"],
        "backup_consecutive_failures": run_state["backup_consecutive_failures"],
    }
