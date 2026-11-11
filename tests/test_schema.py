"""schema：建表齐全 + memories 只追加由数据库层 RAISE(ABORT) 触发器强制。"""

import sqlite3

import pytest

from app.schema import SCHEMA_SQL, init_db


def test_all_tables_created(db, db_path):
    conn = sqlite3.connect(db_path)
    try:
        names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','trigger')"
            )
        }
    finally:
        conn.close()
    expected_tables = {
        "memories",
        "supersedes",
        "derive_state",
        "embeddings",
        "entities",
        "memories_fts",
        "meta",
    }
    assert expected_tables <= names
    assert "memories_no_update" in names
    assert "memories_no_delete" in names


def test_init_db_idempotent(db):
    init_db(db)  # 重复执行不报错
    assert db.read_conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0


def _insert_one(db, memory_id="m0"):
    with db.write_txn() as conn:
        conn.execute(
            "INSERT INTO memories (id, client_write_id, content, recorded_at,"
            " session_id, seq, source_client) VALUES (?,?,?,?,?,?,?)",
            (memory_id, "cw-" + memory_id, "x", 1, "s1", 0, "t"),
        )


def test_update_is_blocked_by_trigger(db):
    _insert_one(db)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        with db.write_txn() as conn:
            conn.execute("UPDATE memories SET content='y' WHERE id='m0'")


def test_delete_is_blocked_by_trigger(db):
    _insert_one(db)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        with db.write_txn() as conn:
            conn.execute("DELETE FROM memories WHERE id='m0'")


def test_event_time_pair_check_constraint(db):
    with pytest.raises(sqlite3.IntegrityError):
        with db.write_txn() as conn:
            conn.execute(
                "INSERT INTO memories (id, client_write_id, content, recorded_at,"
                " session_id, seq, event_time, event_time_precision, source_client)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                ("m1", "cw1", "x", 1, "s1", 1, "2026", None, "t"),
            )


def test_fts_index_populated_on_insert(db):
    _insert_one(db, "mfts")
    with db.write_txn() as conn:
        conn.execute(
            "INSERT INTO memories (id, client_write_id, content, recorded_at,"
            " session_id, seq, source_client) VALUES (?,?,?,?,?,?,?)",
            ("mfts2", "cw-mfts2", "unique-token-xyz", 2, "s1", 1, "t"),
        )
    hit = db.read_conn.execute(
        "SELECT COUNT(*) FROM memories_fts WHERE memories_fts MATCH ?",
        ('"unique-token-xyz"',),
    ).fetchone()[0]
    assert hit == 1


def test_no_update_delete_statement_in_app_code():
    """代码审查断言：app/ 内任何 .py 源码不得对 memories 表执行 UPDATE/DELETE。"""
    import re
    from pathlib import Path

    app_dir = Path(__file__).resolve().parent.parent / "app"
    pattern = re.compile(
        r"(UPDATE|DELETE\s+FROM)\s+memories\b", re.IGNORECASE
    )
    offenders = []
    for py in app_dir.glob("*.py"):
        text = py.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{py.name}:{lineno}: {line.strip()}")
    assert not offenders, offenders
    # SCHEMA_SQL 中的 FTS 同步触发器是外部内容表标准形态，且 UPDATE/DELETE 已被
    # RAISE(ABORT) 触发器拦截（见上），不构成事实层写路径。


# ---- supersedes schema / 迁移 ------------------------------------------------

LEGACY_SUPERSEDES_SQL = """
CREATE TABLE supersedes (
  from_id    TEXT NOT NULL,
  to_id      TEXT NOT NULL,
  kind       TEXT NOT NULL CHECK (kind IN ('supersede','revoke')),
  reason     TEXT,
  recorded_at INTEGER NOT NULL,
  source_client TEXT NOT NULL,
  PRIMARY KEY (from_id, to_id, recorded_at)
);
CREATE INDEX idx_supersedes_to ON supersedes(to_id, recorded_at DESC);
"""


def test_supersedes_uses_autoincrement_id_pk(db):
    cols = {
        r["name"]: r
        for r in db.read_conn.execute("PRAGMA table_info(supersedes)").fetchall()
    }
    assert cols["id"]["pk"] == 1
    # 旧三列主键必须降级为普通列
    assert cols["from_id"]["pk"] == 0
    assert cols["to_id"]["pk"] == 0
    assert cols["recorded_at"]["pk"] == 0


def test_migrates_legacy_supersedes_table(tmp_path):
    from app.db import Database
    from app.schema import init_db, migrate_supersedes_pk

    path = tmp_path / "legacy.db"
    raw = sqlite3.connect(path)
    try:
        raw.executescript(LEGACY_SUPERSEDES_SQL)
        raw.executemany(
            "INSERT INTO supersedes "
            "(from_id, to_id, kind, reason, recorded_at, source_client) "
            "VALUES (?,?,?,?,?,?)",
            [
                ("a", "b", "supersede", "old", 100, "t"),
                ("a", "b", "revoke", None, 200, "t"),
            ],
        )
        raw.commit()
    finally:
        raw.close()

    database = Database(str(path))
    try:
        assert migrate_supersedes_pk(database) is True
        init_db(database)

        cols = {
            r["name"]: r
            for r in database.read_conn.execute(
                "PRAGMA table_info(supersedes)"
            ).fetchall()
        }
        assert cols["id"]["pk"] == 1

        rows = database.read_conn.execute(
            "SELECT id, from_id, to_id, kind, recorded_at FROM supersedes ORDER BY id"
        ).fetchall()
        assert [r["kind"] for r in rows] == ["supersede", "revoke"]
        assert [r["recorded_at"] for r in rows] == [100, 200]

        # 旧主键下会撞 PK 的同毫秒第三条边，现在可以正常追加
        with database.write_txn() as conn:
            conn.execute(
                "INSERT INTO supersedes "
                "(from_id, to_id, kind, reason, recorded_at, source_client) "
                "VALUES (?,?,?,?,?,?)",
                ("a", "b", "supersede", None, 200, "t"),
            )
        assert (
            database.read_conn.execute("SELECT COUNT(*) FROM supersedes").fetchone()[0]
            == 3
        )

        # 迁移幂等：再次 init_db 不重建、不丢数据
        assert migrate_supersedes_pk(database) is False
        init_db(database)
        assert (
            database.read_conn.execute("SELECT COUNT(*) FROM supersedes").fetchone()[0]
            == 3
        )
    finally:
        database.close()


# ---- v8 §6 gseq / 迁移 / VACUUM ----------------------------------------------

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


def test_memories_has_gseq_pk_and_id_unique(db):
    cols = {
        r["name"]: r
        for r in db.read_conn.execute("PRAGMA table_info(memories)").fetchall()
    }
    assert cols["gseq"]["pk"] == 1
    assert cols["gseq"]["type"] == "INTEGER"
    assert cols["id"]["pk"] == 0
    assert cols["id"]["notnull"] == 1
    # id 仍是 UNIQUE
    indexes = {
        r["name"]: r
        for r in db.read_conn.execute("PRAGMA index_list(memories)").fetchall()
    }
    assert any("memories_fts" not in name for name in indexes)


def test_migrate_legacy_memories_fills_gseq_from_rowid(tmp_path):
    from app.db import Database
    from app.schema import init_db, migrate_memories_gseq

    path = tmp_path / "legacy-memories.db"
    raw = sqlite3.connect(path)
    try:
        raw.executescript(LEGACY_MEMORIES_SQL)
        raw.executemany(
            "INSERT INTO memories (id, client_write_id, content, recorded_at,"
            " session_id, seq, source_client) VALUES (?,?,?,?,?,?,?)",
            [
                ("id-a", "cw-a", "first", 100, "s1", 0, "t"),
                ("id-b", "cw-b", "second", 200, "s1", 1, "t"),
                ("id-c", "cw-c", "third", 300, "s2", 0, "t"),
            ],
        )
        raw.commit()
    finally:
        raw.close()

    database = Database(str(path))
    try:
        assert migrate_memories_gseq(database) is True
        init_db(database)
        rows = database.read_conn.execute(
            "SELECT gseq, id, seq FROM memories ORDER BY gseq"
        ).fetchall()
        assert [(r["gseq"], r["id"], r["seq"]) for r in rows] == [
            (1, "id-a", 0),
            (2, "id-b", 1),
            (3, "id-c", 0),
        ]
        # seq 语义不变；id 仍是对外稳定标识
        assert [r["seq"] for r in rows] == [0, 1, 0]
        # FTS 已重建（external content rowid 稳定）
        hit = database.read_conn.execute(
            "SELECT COUNT(*) FROM memories_fts WHERE memories_fts MATCH 'second'"
        ).fetchone()[0]
        assert hit == 1
        # 触发器与索引已重建
        names = {
            r[0]
            for r in database.read_conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('trigger','index')"
            )
        }
        assert {"memories_no_update", "memories_no_delete", "memories_fts_ai"} <= names
        assert {"idx_memories_session", "idx_memories_time"} <= names
        # sqlite_sequence 已推进到 max(gseq)，新写入从 4 开始
        seq_value = database.read_conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='memories'"
        ).fetchone()[0]
        assert seq_value == 3
        from app.write_path import create_memory

        receipt = create_memory(
            database,
            client_write_id="cw-new",
            content="fourth",
            session_id="s1",
            source_client="t",
        )
        assert receipt["seq"] == 2  # 会话内 seq 继续从 1 之后
        gseq = database.read_conn.execute(
            "SELECT gseq FROM memories WHERE id=?", (receipt["id"],)
        ).fetchone()[0]
        assert gseq == 4
        # 迁移是幂等的
        assert migrate_memories_gseq(database) is False
    finally:
        database.close()


def test_vacuum_into_preserves_gseq(db, db_path, tmp_path):
    from app.write_path import create_memory

    ids = []
    for i in range(5):
        receipt = create_memory(
            db,
            client_write_id=f"cw-vac-{i}",
            content=f"vacuum {i}",
            session_id="sv",
            source_client="t",
        )
        ids.append(receipt["id"])
    before = [
        (r["gseq"], r["id"])
        for r in db.read_conn.execute("SELECT gseq, id FROM memories ORDER BY gseq")
    ]
    assert [g for g, _ in before] == [1, 2, 3, 4, 5]

    dest = str(tmp_path / "vacuumed.db")
    db.vacuum_into(dest)
    conn = sqlite3.connect(dest)
    try:
        after = conn.execute("SELECT gseq, id FROM memories ORDER BY gseq").fetchall()
        sequence = conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='memories'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert after == before
    assert sequence == 5
    assert [mid for _, mid in after] == ids


def test_supersedes_pair_index_is_id_only(db):
    row = db.read_conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_supersedes_pair'"
    ).fetchone()
    sql = (row["sql"] or "").lower()
    assert "id desc" in sql
    assert "recorded_at" not in sql
