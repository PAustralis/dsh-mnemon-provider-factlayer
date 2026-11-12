"""/health 报告：派生水位、备份状态、生效配置快照、hidden 大小、各路空结果率、熔断、金丝雀。"""

from __future__ import annotations

import json

from . import voided as voided_mod
from .contract import PROVIDER_MANIFEST
from .db import Database
from .derive import STAGES, DerivePipeline
from .maintenance import fts_optimize_state
from .schema import (
    MIGRATION_NEEDS_REBUILD_KEY,
    MIGRATION_REBUILD_KEY,
    get_meta,
)

LAST_BACKUP_AT_KEY = "last_backup_at"


def sqlite_state(db: Database) -> dict:
    """SQLite 文件/WAL/检查点可观测状态（后台快照里刷新）。"""
    sizes = db.file_sizes()
    checkpoint = db.checkpoint_state()
    return {
        "db_bytes": sizes["db_bytes"],
        "wal_bytes": sizes["wal_bytes"],
        "wal_autocheckpoint": db.wal_autocheckpoint_pages,
        "last_checkpoint_at": checkpoint["last_checkpoint_at"],
        "last_checkpoint_mode": checkpoint["last_checkpoint_mode"],
        # 失败（尤其 TRUNCATE 被常驻读者挡住）必须可观测，不能静默。
        "last_checkpoint_error": checkpoint["last_checkpoint_error"],
        "last_checkpoint_result": checkpoint["last_checkpoint_result"],
    }


def last_backup_at(db: Database) -> int | None:
    raw = get_meta(db, LAST_BACKUP_AT_KEY)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def migration_state(db: Database) -> dict:
    """迁移后派生重建状态（init_db 写入 meta；失败时必须让 /health 可见）。"""
    needs = get_meta(db, MIGRATION_NEEDS_REBUILD_KEY) == "1"
    last: dict | None = None
    raw = get_meta(db, MIGRATION_REBUILD_KEY)
    if raw:
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            parsed = {"status": "unknown", "raw": raw}
        last = parsed if isinstance(parsed, dict) else {"status": "unknown"}
    return {"needs_rebuild": needs, "last": last}


def index_row_counts(db: Database) -> dict:
    """事实行数 vs 派生索引行数：三者不等 = 有记忆搜不到。"""
    conn = db.read_conn
    memories = int(conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
    ngrams = int(conn.execute("SELECT COUNT(*) FROM memories_ngrams").fetchone()[0])
    bigram = int(conn.execute("SELECT COUNT(*) FROM memories_fts_bigram").fetchone()[0])
    gap = max(0, memories - ngrams, memories - bigram)
    return {
        "memories": memories,
        "ngrams": ngrams,
        "bigram_fts": bigram,
        "gap": gap,
    }


def health_report(
    db: Database,
    pipeline: DerivePipeline,
    backup_scheduler=None,
    retrieval=None,
    maintenance=None,
) -> dict:
    """备份状态来自调度器；没有调度器（测试/只读用法）时报告「无失败」。

    检索层接入后（``retrieval`` 非 None）额外暴露（v8 §5/F）：
    - 生效配置快照（模型名/维度/启用路线/契约常量/开关）
    - hidden 大小（全系统唯一口径）
    - 各路最近空结果率、被隐藏替换计数、出口断言丢弃计数
    - 向量路熔断状态
    - 金丝雀最近结果（由 /health 调用 run_canary 填入）
    """
    run_state = (
        backup_scheduler.status()
        if backup_scheduler is not None
        else {"last_backup_error": None, "backup_consecutive_failures": 0}
    )
    pending = {stage: pipeline.pending_count(stage) for stage in STAGES}
    embedding = pipeline.embedding_state()
    migration = migration_state(db)
    index = index_row_counts(db)
    alerts: list[str] = []
    if migration["needs_rebuild"]:
        # 需求口径：迁移后重建失败时 /health 必须暴露「pending fts > 0」告警
        if pending["fts"] > 0:
            alerts.append("pending fts > 0")
        alerts.append("migration_needs_rebuild")
    if index["gap"] > 0:
        # 任何原因的索引缺口：事实层有、ngram/bigram 没有 = 有记忆搜不到
        alerts.append(f"fts index gap: {index['gap']}")
    if embedding["failures"] > 0:
        # embedding 失败是静默通道：必须让 /health 的 alerts 直接可见。
        alerts.append("embedding_failures>0")
    if embedding["circuit"]["circuit_open"]:
        alerts.append("embedding_circuit_open")
    report = {
        "pending": pending,
        "failures": {stage: pipeline.failures[stage] for stage in STAGES},
        "embedding": embedding,
        "last_backup_at": last_backup_at(db),
        "last_backup_error": run_state["last_backup_error"],
        "backup_consecutive_failures": run_state["backup_consecutive_failures"],
        "migration_needs_rebuild": migration["needs_rebuild"],
        "migration": migration["last"],
        "index": index,
        "sqlite": sqlite_state(db),
        "fts_optimize": fts_optimize_state(db),
        "alerts": alerts,
    }
    if maintenance is not None:
        # 「后台维护线程到底起没起、checkpoint/optimize 是否启用」必须可观测：
        # 只靠日志无法在 /health 上回答「为什么这个库从不 checkpoint」。
        report["maintenance"] = maintenance.state()
    if retrieval is not None:
        snapshot = retrieval.cfg.snapshot()
        snapshot["effective_routes"] = list(retrieval.routes)
        stats = retrieval.stats.snapshot()
        stats["degraded"] = bool(retrieval.degrade.get("degraded"))
        stats["degrade_reason"] = retrieval.degrade.get("reason")
        stats["hidden_size"] = len(voided_mod.hidden_id_set(db.read_conn))
        stats["circuit"] = retrieval.vector_state()
        stats["canary"] = retrieval.canary_state()
        report["config"] = snapshot
        report["retrieval"] = stats
        report["provider_manifest"] = PROVIDER_MANIFEST
    return report
