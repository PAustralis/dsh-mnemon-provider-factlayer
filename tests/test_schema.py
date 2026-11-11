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
