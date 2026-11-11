"""db.write_txn：COMMIT 失败必须回滚，否则连接卡在打开事务态导致后续写入永久失败。"""

import sqlite3

import pytest


def test_commit_failure_rolls_back_and_later_write_succeeds(db, monkeypatch):
    original_commit = db._commit
    state = {"should_fail": True}

    def flaky_commit(conn):
        if state["should_fail"]:
            state["should_fail"] = False
            raise sqlite3.OperationalError("disk I/O error")
        original_commit(conn)

    monkeypatch.setattr(db, "_commit", flaky_commit)

    with pytest.raises(sqlite3.OperationalError):
        with db.write_txn() as conn:
            conn.execute("INSERT INTO meta(key, value) VALUES('k', 'v')")

    # 关键回归：连接不能停在打开事务里
    assert db._write_conn.in_transaction is False
    assert (
        db.read_conn.execute("SELECT COUNT(*) FROM meta WHERE key='k'").fetchone()[0]
        == 0
    ), "COMMIT 失败的事务必须被回滚"

    # 后续写事务必须仍能 BEGIN IMMEDIATE 并成功提交
    with db.write_txn() as conn:
        conn.execute("INSERT INTO meta(key, value) VALUES('k', 'v')")
    assert (
        db.read_conn.execute("SELECT value FROM meta WHERE key='k'").fetchone()[0]
        == "v"
    )
