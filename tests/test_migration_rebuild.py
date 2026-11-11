"""旧库迁移后**强制派生重建**（v8 审阅 P0：绝不静默丢数据）。

背景：``migrate_memories_gseq()`` 只重建了原文 FTS（``memories_fts``），而后加的
``memories_ngrams`` / ``memories_fts_bigram`` 在 legacy 库迁移后是空表；默认派生
循环关闭、``/derive/tick`` 每轮只处理 50 条 → 旧记忆会永久搜不到且无告警。

本文件断言：legacy 库（含中文内容）→ ``init_db`` → **经 bigram 路真的能检索到**；
重建失败 → ERROR 日志 + ``/health`` 的 ``migration_needs_rebuild`` / ``pending fts``
告警；无 embedding 配置时跳过向量并记录原因（不报错）。
"""

from __future__ import annotations

import json
import logging
import sqlite3

from app.db import Database
from app.derive import DerivePipeline
from app.health import health_report
from app.schema import (
    MIGRATION_NEEDS_REBUILD_KEY,
    MIGRATION_REBUILD_KEY,
    get_meta,
    init_db,
)
from tests.retrieval_helpers import make_service

# 与 test_schema.py 的 LEGACY_MEMORIES_SQL 一致：v3.1 及以前的 memories 结构
LEGACY_MEMORIES_SQL = """
CREATE TABLE memories (
  id           TEXT PRIMARY KEY,
  client_write_id TEXT NOT NULL UNIQUE,
  content      TEXT NOT NULL,
  recorded_at  INTEGER NOT NULL,
  session_id   TEXT NOT NULL,
  seq          INTEGER NOT NULL,
  event_time   TEXT,
  event_time_precision TEXT CHECK (event_time_precision IN ('year','month','day','instant')),
  source_client TEXT NOT NULL,
  CHECK ((event_time IS NULL) = (event_time_precision IS NULL))
);
CREATE INDEX idx_memories_session ON memories(session_id, seq);
CREATE INDEX idx_memories_time ON memories(recorded_at DESC);
CREATE TRIGGER memories_no_update BEFORE UPDATE ON memories
BEGIN SELECT RAISE(ABORT, 'memories is append-only: UPDATE forbidden'); END;
CREATE TRIGGER memories_no_delete BEFORE DELETE ON memories
BEGIN SELECT RAISE(ABORT, 'memories is append-only: DELETE forbidden'); END;
CREATE VIRTUAL TABLE memories_fts USING fts5(
  content, content='memories', content_rowid='rowid'
);
CREATE TRIGGER memories_fts_ai AFTER INSERT ON memories BEGIN
  INSERT INTO memories_fts(rowid, content) VALUES (new.rowid, new.content);
END;
"""

LEGACY_ROWS = [
    ("legacy-beijing", "cw-legacy-1", "2026 年上半年我住在北京朝阳区", 1700000000000, "s1", 0, "t"),
    ("legacy-tea", "cw-legacy-2", "我平时喝龙井茶，偶尔也喝普洱", 1700000001000, "s1", 1, "t"),
]


def _make_legacy_db(path, rows=LEGACY_ROWS) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(LEGACY_MEMORIES_SQL)
        conn.executemany(
            "INSERT INTO memories (id, client_write_id, content, recorded_at,"
            " session_id, seq, source_client) VALUES (?,?,?,?,?,?,?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def _counts(db: Database) -> tuple[int, int, int]:
    conn = db.read_conn
    return (
        conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM memories_ngrams").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM memories_fts_bigram").fetchone()[0],
    )


def test_legacy_db_init_rebuilds_bigram_index_and_is_searchable(tmp_path):
    path = tmp_path / "legacy.db"
    _make_legacy_db(path)

    db = Database(str(path))
    try:
        report = init_db(db)

        assert report.migrated_memories is True
        assert report.rebuild_needed is True
        assert report.rebuild_attempted is True
        assert report.rebuild_ok is True
        # 迁移只重建原文 FTS 的漏网场景：ngram/bigram 必须补齐到与事实行数相等
        memories, ngrams, bigram = _counts(db)
        assert memories == len(LEGACY_ROWS) == 2
        assert ngrams == memories
        assert bigram == memories
        # 未配置 embedding：跳过并记录原因，不报错；旧 embeddings 行也不动
        assert report.embedding_skipped and "embedding" in report.embedding_skipped
        assert report.embedding_error is None
        assert get_meta(db, MIGRATION_NEEDS_REBUILD_KEY) == "0"
        detail = json.loads(get_meta(db, MIGRATION_REBUILD_KEY))
        assert detail["status"] == "ok"
        assert detail["counts"]["fts"] == 2
        assert detail["embedding_skipped"]

        # 关键断言：不是「原文 FTS 能搜到」，而是**经 bigram 路可检索到**
        service = make_service(db, routes=("bigram",))
        assert "legacy-beijing" in service.bigram_route("北京", 50).raw_ids
        result = service.search("北京")
        assert "legacy-beijing" in [item["id"] for item in result["items"]]

        health = health_report(db, DerivePipeline(db, stub_success=True))
        assert health["migration_needs_rebuild"] is False
        assert health["index"]["gap"] == 0
        assert health["alerts"] == []

        # 幂等：索引已补齐后再 init_db 不再重复重建
        again = init_db(db)
        assert again.rebuild_attempted is False
    finally:
        db.close()


def test_index_gap_without_fresh_migration_also_rebuilds(tmp_path):
    """先手工 migrate_memories_gseq、再 init_db：migration 返回 False，但缺口仍要补。"""
    from app.schema import migrate_memories_gseq

    path = tmp_path / "manual-migrate.db"
    _make_legacy_db(path)
    db = Database(str(path))
    try:
        assert migrate_memories_gseq(db) is True
        report = init_db(db)
        assert report.migrated_memories is False
        assert report.rebuild_attempted is True and report.rebuild_ok is True
        memories, ngrams, bigram = _counts(db)
        assert ngrams == bigram == memories == 2
        result = make_service(db, routes=("bigram",)).search("龙井茶")
        assert "legacy-tea" in [item["id"] for item in result["items"]]
    finally:
        db.close()


def _failed_rebuild_pipeline(db: Database) -> DerivePipeline:
    pipe = DerivePipeline(db, stub_success=True)

    def boom() -> dict:
        raise RuntimeError("simulated rebuild failure: disk full")

    pipe.rebuild_after_migration = boom  # type: ignore[method-assign]
    return pipe


def test_rebuild_failure_is_logged_and_exposed_in_health(tmp_path, caplog):
    path = tmp_path / "legacy-fail.db"
    _make_legacy_db(path)
    db = Database(str(path))
    try:
        pipe = _failed_rebuild_pipeline(db)
        caplog.set_level(logging.ERROR, logger="factlayer.schema")
        report = init_db(db, pipeline=pipe)

        assert report.rebuild_attempted is True
        assert report.rebuild_ok is False
        assert "disk full" in report.rebuild_error
        assert get_meta(db, MIGRATION_NEEDS_REBUILD_KEY) == "1"
        # 重建失败不留半成品：派生层保持为空，等 /admin/rebuild 重试
        assert _counts(db) == (2, 0, 0)
        errors = [rec for rec in caplog.records if rec.levelno == logging.ERROR]
        assert any("重建失败" in rec.getMessage() for rec in errors)

        health = health_report(db, pipe)
        assert health["migration_needs_rebuild"] is True
        assert health["migration"]["status"] == "failed"
        assert health["pending"]["fts"] == 2
        assert "pending fts > 0" in health["alerts"]
        assert "migration_needs_rebuild" in health["alerts"]
        assert health["index"]["gap"] == 2
    finally:
        db.close()


def test_create_app_migrated_legacy_db_searchable(tmp_path, monkeypatch):
    """create_app 也要把迁移后重建跑完（走配置好的 pipeline / provider）。"""
    from fastapi.testclient import TestClient

    from app.main import create_app

    path = tmp_path / "legacy-app.db"
    _make_legacy_db(path)
    monkeypatch.setenv("DSH_DERIVE_LOOP_ENABLED", "0")
    monkeypatch.setenv("DSH_BACKUP_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("RETRIEVAL_ROUTES", "bigram")

    app = create_app(db_path=str(path))
    with TestClient(app) as client:
        result = client.post("/search", json={"query": "北京"}).json()
        assert result["count"] >= 1
        assert "legacy-beijing" in [item["id"] for item in result["items"]]
        health = client.get("/health").json()
        assert health["migration_needs_rebuild"] is False
        assert health["index"]["memories"] == health["index"]["ngrams"] == 2
        assert health["alerts"] == []
