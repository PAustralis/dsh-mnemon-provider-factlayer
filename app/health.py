"""/health 报告：各 stage 未派生条数、失败计数、备份最后成功时间。"""

from __future__ import annotations

from .db import Database
from .derive import DerivePipeline, STAGES
from .schema import get_meta

LAST_BACKUP_AT_KEY = "last_backup_at"


def health_report(db: Database, pipeline: DerivePipeline) -> dict:
    return {
        "pending": {stage: pipeline.pending_count(stage) for stage in STAGES},
        "failures": {stage: pipeline.failures[stage] for stage in STAGES},
        "last_backup_at": _last_backup_at(db),
    }


def _last_backup_at(db: Database) -> int | None:
    raw = get_meta(db, LAST_BACKUP_AT_KEY)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None
