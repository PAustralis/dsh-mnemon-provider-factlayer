"""SQLite 运维：WAL 检查点、FTS5 optimize、派生重建批量插入（2026-11-12）。"""

from __future__ import annotations

import logging
import sqlite3

from fastapi.testclient import TestClient

from app.config import load_config
from app.db import Database
from app.derive import DerivePipeline
from app.main import create_app
from app.maintenance import Maintenance, optimize_fts
from app.schema import init_db
from app.write_path import create_memory
from tests.conftest import prime_health, write_one


class _RecordingConn:
    """包一层写连接：记录 execute 的 SQL，用于断言重建不再逐条 INSERT。"""

    def __init__(self, real):
        self._real = real
        self.executed: list[str] = []

    def execute(self, sql, params=()):
        self.executed.append(sql)
        return self._real.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _derived_snapshot(db: Database) -> dict:
    conn = db.read_conn
    return {
        "ngrams": [
            tuple(row)
            for row in conn.execute(
                "SELECT memory_id, ngram_text FROM memories_ngrams ORDER BY memory_id"
            ).fetchall()
        ],
        "bigram": [
            tuple(row)
            for row in conn.execute(
                "SELECT rowid, ngram_text FROM memories_fts_bigram ORDER BY rowid"
            ).fetchall()
        ],
        "trigram": [
            tuple(row)
            for row in conn.execute(
                "SELECT rowid, content FROM memories_fts_trigram ORDER BY rowid"
            ).fetchall()
        ],
        "embeddings": [
            tuple(row)
            for row in conn.execute(
                "SELECT memory_id, model_id, dim FROM embeddings ORDER BY memory_id"
            ).fetchall()
        ],
        # derive_state 的 done_at 是本次重建时间，不属于「结果」；比较水位集合。
        "derive_state": [
            tuple(row)
            for row in conn.execute(
                "SELECT memory_id, stage FROM derive_state ORDER BY memory_id, stage"
            ).fetchall()
        ],
    }


# ---------------------------------------------------------------- WAL 检查点


def test_wal_autocheckpoint_pragma_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DSH_WAL_AUTOCHECKPOINT_PAGES", "250")
    database = Database(str(tmp_path / "wal-pragma.db"))
    try:
        assert database.wal_autocheckpoint_pages == 250
        for conn in (database._write_conn, database.read_conn):
            value = conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0]
            assert value == 250, "每个连接都必须显式设置 wal_autocheckpoint"
    finally:
        database.close()


def test_wal_autocheckpoint_defaults_to_1000(tmp_path, monkeypatch):
    monkeypatch.delenv("DSH_WAL_AUTOCHECKPOINT_PAGES", raising=False)
    database = Database(str(tmp_path / "wal-default.db"))
    try:
        assert database.wal_autocheckpoint_pages == 1000
        assert (
            database.read_conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0]
            == 1000
        )
    finally:
        database.close()


def test_admin_checkpoint_returns_structure_and_health_sqlite(client):
    write_one(client, cwid="cw-cp-1", content="检查点测试", session_id="s-cp")
    resp = client.post("/admin/checkpoint")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"busy", "log_pages", "checkpointed_pages"}
    assert all(isinstance(value, int) for value in body.values())
    assert body["busy"] == 0, f"PASSIVE 不应被空载读者阻塞：{body}"

    health = prime_health(client)
    sqlite_state = health["sqlite"]
    for key in (
        "db_bytes",
        "wal_bytes",
        "wal_autocheckpoint",
        "last_checkpoint_at",
        "last_checkpoint_mode",
        "last_checkpoint_error",
    ):
        assert key in sqlite_state
    assert sqlite_state["wal_autocheckpoint"] == 1000
    assert sqlite_state["last_checkpoint_mode"] == "PASSIVE"
    assert isinstance(sqlite_state["last_checkpoint_at"], int)
    assert sqlite_state["last_checkpoint_error"] is None
    assert sqlite_state["db_bytes"] > 0
    assert sqlite_state["wal_bytes"] >= 0


def test_health_wal_bytes_grows_with_writes(client):
    before = prime_health(client)["sqlite"]["wal_bytes"]
    for i in range(20):
        resp = write_one(
            client,
            cwid=f"cw-wal-grow-{i}",
            content=("WAL 增长测试 " + "x" * 4000 + f" #{i}"),
            session_id="s-wal-grow",
        )
        assert resp.status_code == 200
    after = prime_health(client)["sqlite"]
    assert after["wal_bytes"] > before, "WAL 应随写入增长（自动检查点阈值 1000 页）"
    assert after["db_bytes"] > 0


def test_truncate_blocked_by_reader_recorded_not_raised(db, db_path):
    create_memory(
        db,
        client_write_id="cw-cp-reader",
        content="读者占用时的 TRUNCATE",
        session_id="s-cp-reader",
        source_client="t",
    )
    reader = sqlite3.connect(db_path)
    try:
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM memories").fetchone()
        result = db.wal_checkpoint("TRUNCATE")  # 不得抛异常
    finally:
        reader.rollback()
        reader.close()

    assert result["busy"] == 1
    state = db.checkpoint_state()
    assert state["last_checkpoint_mode"] == "TRUNCATE"
    assert state["last_checkpoint_at"] is not None
    assert state["last_checkpoint_error"]
    assert "busy=1" in state["last_checkpoint_error"]
    # /health 的 sqlite 段必须能看到这次失败（不得静默）
    from app.health import sqlite_state

    assert sqlite_state(db)["last_checkpoint_error"] == state["last_checkpoint_error"]


def test_background_maintenance_truncate_busy_is_logged_not_raised(db, db_path):
    create_memory(
        db,
        client_write_id="cw-cp-bg",
        content="后台维护检查点",
        session_id="s-cp-bg",
        source_client="t",
    )
    now = [0.0]
    maintenance = Maintenance(
        db,
        checkpoint_interval_seconds=1,
        checkpoint_idle_seconds=0,
        fts_optimize_interval_seconds=0,
        clock=lambda: now[0],
    )
    now[0] = 2.0
    reader = sqlite3.connect(db_path)
    try:
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM memories").fetchone()
        result = maintenance.maybe_checkpoint_truncate()
    finally:
        reader.rollback()
        reader.close()

    assert result is not None
    assert result["busy"] == 1
    assert db.last_checkpoint_error
    assert db.last_checkpoint_mode == "TRUNCATE"


def test_admin_checkpoint_rejects_unknown_mode(client):
    resp = client.post("/admin/checkpoint", params={"mode": "NO_SUCH_MODE"})
    assert resp.status_code == 422


def test_admin_checkpoint_requires_token_when_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "checkpoint-token")
    monkeypatch.setenv("DSH_DERIVE_LOOP_ENABLED", "0")
    monkeypatch.setenv("DSH_BACKUP_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("RETRIEVAL_ROUTES", "bigram")
    for name in (
        "EMBEDDING_MODEL",
        "DSH_EMBEDDING_MODEL",
        "EMBEDDING_BASE_URL",
        "DSH_EMBEDDING_BASE_URL",
        "DSH_EMBEDDING_API_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    app = create_app(db_path=str(tmp_path / "checkpoint-token.db"))
    with TestClient(app) as client:
        assert client.post("/admin/checkpoint").status_code == 403
        ok = client.post(
            "/admin/checkpoint", headers={"X-Admin-Token": "checkpoint-token"}
        )
        assert ok.status_code == 200
        assert set(ok.json()) == {"busy", "log_pages", "checkpointed_pages"}


# ---------------------------------------------------------------- FTS5 optimize


def test_admin_rebuild_optimizes_all_fts_tables(client, monkeypatch):
    for i in range(2):
        write_one(
            client,
            cwid=f"cw-opt-{i}",
            content=f"FTS optimize 测试 {i}",
            session_id="s-opt",
        )
    import app.maintenance as maintenance_mod

    calls: list[str] = []
    original = maintenance_mod._run_optimize

    def counting(conn, table):
        calls.append(table)
        return original(conn, table)

    monkeypatch.setattr(maintenance_mod, "_run_optimize", counting)
    resp = client.post("/admin/rebuild")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["fts_optimize"]["ok"] is True
    assert set(calls) == {
        "memories_fts",
        "memories_fts_bigram",
        "memories_fts_trigram",
    }

    health = prime_health(client)
    assert isinstance(health["fts_optimize"]["last_at"], int)
    assert health["fts_optimize"]["last_error"] is None


def test_optimize_failure_is_recorded_in_health(client, monkeypatch):
    write_one(client, cwid="cw-opt-fail", content="optimize 失败", session_id="s-optf")
    import app.maintenance as maintenance_mod

    def boom(conn, table):
        raise sqlite3.OperationalError(f"optimize boom: {table}")

    monkeypatch.setattr(maintenance_mod, "_run_optimize", boom)
    resp = client.post("/admin/rebuild")
    assert resp.status_code == 200, "optimize 失败不得让 /admin/rebuild 失败"
    assert resp.json()["fts_optimize"]["ok"] is False

    health = prime_health(client)
    assert health["fts_optimize"]["last_at"] is not None
    assert "optimize boom" in (health["fts_optimize"]["last_error"] or "")


def test_migration_rebuild_also_optimizes_fts(tmp_path, monkeypatch):
    from tests.test_migration_rebuild import _make_legacy_db

    path = tmp_path / "legacy-optimize.db"
    _make_legacy_db(path)
    database = Database(str(path))
    try:
        import app.maintenance as maintenance_mod

        calls: list[str] = []
        original = maintenance_mod._run_optimize

        def counting(conn, table):
            calls.append(table)
            return original(conn, table)

        monkeypatch.setattr(maintenance_mod, "_run_optimize", counting)
        report = init_db(database)
        assert report.rebuild_ok is True
        assert calls, "迁移强制重建后必须执行 FTS5 optimize"
        assert "memories_fts" in calls
    finally:
        database.close()


def test_maintenance_optimize_respects_interval(db):
    now = [100.0]
    maintenance = Maintenance(
        db,
        checkpoint_interval_seconds=0,
        fts_optimize_interval_seconds=10,
        wall_clock=lambda: now[0],
    )
    assert maintenance.maybe_optimize_fts() is None  # 启动时先等一个完整间隔
    now[0] = 111.0
    result = maintenance.maybe_optimize_fts()
    assert result is not None and result["ok"] is True
    now[0] = 115.0
    assert maintenance.maybe_optimize_fts() is None  # 距上次不足 10s


# ---------------------------------------------------------------- 批量插入


def test_rebuild_uses_executemany_batches_and_is_idempotent(
    client, db, monkeypatch, caplog
):
    for i in range(7):
        write_one(
            client,
            cwid=f"cw-batch-{i}",
            content=f"批量重建测试 {i}",
            session_id="s-batch",
        )
    pipeline = DerivePipeline(db, stub_success=True, batch_size=3)
    calls: list[tuple[str, int]] = []
    original = db.executemany

    def counting(conn, sql, params):
        materialized = list(params)
        calls.append((sql, len(materialized)))
        return original(conn, sql, materialized)

    monkeypatch.setattr(db, "executemany", counting)
    caplog.set_level(logging.INFO, logger="factlayer.derive")
    recording = _RecordingConn(db._write_conn)
    db._write_conn = recording
    try:
        counts = pipeline.rebuild_all()
        first = _derived_snapshot(db)
        pipeline.rebuild_all()  # 重复重建：结果必须一致（幂等）
        second = _derived_snapshot(db)
    finally:
        db._write_conn = recording._real

    assert counts == {"fts": 7, "embedding": 7, "entity": 7, "graph": 7}

    sqls = " ".join(sql for sql, _ in calls)
    for table in (
        "memories_ngrams",
        "memories_fts_bigram",
        "memories_fts_trigram",
        "derive_state",
    ):
        assert table in sqls, f"{table} 必须走 executemany 批量写入"
    ngram_batches = [
        size for sql, size in calls if "INSERT INTO memories_ngrams" in sql
    ]
    assert ngram_batches == [3, 3, 1, 3, 3, 1], "应按 batch_size=3 分批"
    assert "batch_size=3" in caplog.text
    # 关键回归：重建不再通过 execute 逐条 INSERT（全部走 executemany）。
    assert not [
        sql
        for sql in recording.executed
        if sql.lstrip().upper().startswith("INSERT")
    ], f"仍有逐条 INSERT：{recording.executed}"
    assert first == second


def test_rebuild_batches_embedding_rows_with_provider(client, db, monkeypatch):
    class FakeProvider:
        model_id = "fake-batch"

        def embed(self, texts):
            return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    for i in range(4):
        write_one(
            client,
            cwid=f"cw-emb-batch-{i}",
            content=f"向量批处理 {i}",
            session_id="s-emb-batch",
        )
    pipeline = DerivePipeline(
        db, embedding_provider=FakeProvider(), batch_size=2
    )
    calls: list[str] = []
    original = db.executemany

    def counting(conn, sql, params):
        calls.append(sql)
        return original(conn, sql, list(params))

    monkeypatch.setattr(db, "executemany", counting)
    counts = pipeline.rebuild_all()
    assert counts["embedding"] == 4
    rows = db.read_conn.execute(
        "SELECT memory_id, model_id, dim FROM embeddings ORDER BY memory_id"
    ).fetchall()
    assert len(rows) == 4
    assert all(row["model_id"] == "fake-batch" and row["dim"] == 4 for row in rows)
    embedding_sql = [
        sql for sql in calls if "INSERT OR REPLACE INTO embeddings" in sql
    ]
    assert embedding_sql, "embeddings 必须走 executemany"
    store = db.read_conn.execute(
        "SELECT value FROM meta WHERE key='embedding_dim'"
    ).fetchone()
    assert store["value"] == "4"


def test_optimize_fts_direct_is_idempotent(db):
    create_memory(
        db,
        client_write_id="cw-opt-direct",
        content="direct optimize",
        session_id="s-opt-direct",
        source_client="t",
    )
    first = optimize_fts(db, reason="test")
    second = optimize_fts(db, reason="test")
    assert first["ok"] is True and second["ok"] is True
    assert set(first["optimized"]) >= {"memories_fts", "memories_fts_bigram"}


def test_load_config_exposes_sqlite_maintenance_defaults(monkeypatch):
    for name in (
        "DSH_WAL_AUTOCHECKPOINT_PAGES",
        "DSH_WAL_CHECKPOINT_INTERVAL_SECONDS",
        "DSH_WAL_CHECKPOINT_IDLE_SECONDS",
        "DSH_FTS_OPTIMIZE_INTERVAL_SECONDS",
        "DSH_DERIVE_BATCH_SIZE",
    ):
        monkeypatch.delenv(name, raising=False)
    cfg = load_config()
    assert cfg.wal_autocheckpoint_pages == 1000
    assert cfg.wal_checkpoint_interval_seconds == 300
    assert cfg.wal_checkpoint_idle_seconds == 60.0
    assert cfg.fts_optimize_interval_seconds == 86400
    assert cfg.derive_batch_size == 500
    assert cfg.snapshot()["wal_autocheckpoint_pages"] == 1000
