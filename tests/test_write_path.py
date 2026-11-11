"""写入路径：回执、幂等、409、event_time 校验、并发 seq 连续、supersedes 边。"""

import concurrent.futures
import sqlite3
import threading

import pytest
from fastapi import HTTPException

from tests.conftest import write_one


def test_write_receipt_shape(client):
    resp = write_one(client, cwid="cw-1", content="hello", session_id="s1")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) >= {"id", "seq", "recorded_at", "client_write_id"}
    assert body["seq"] == 0
    assert body["client_write_id"] == "cw-1"


def test_idempotent_retry_returns_same_record(client):
    first = write_one(client, cwid="cw-retry", content="same", session_id="s1").json()
    resp = write_one(client, cwid="cw-retry", content="same", session_id="s1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == first["id"]
    assert body["seq"] == first["seq"]
    assert body["recorded_at"] == first["recorded_at"]
    assert body.get("duplicated") is True


def test_same_cwid_different_content_conflict_409(client):
    write_one(client, cwid="cw-conflict", content="aaa", session_id="s1")
    resp = write_one(client, cwid="cw-conflict", content="bbb", session_id="s1")
    assert resp.status_code == 409
    assert "content" in resp.json()["detail"]


def test_same_cwid_different_session_conflict_409(client):
    write_one(client, cwid="cw-sess", content="aaa", session_id="s1")
    resp = write_one(client, cwid="cw-sess", content="aaa", session_id="s2")
    assert resp.status_code == 409


def test_duplicate_content_different_cwid_stored_twice(client):
    write_one(client, cwid="cw-d1", content="dup", session_id="s1")
    second = write_one(client, cwid="cw-d2", content="dup", session_id="s1")
    assert second.status_code == 200
    assert second.json()["seq"] == 1  # 两条都存，靠多条记录本身表达重复


# ---- event_time 校验 -------------------------------------------------------


def test_event_time_only_one_of_pair_rejected(client):
    resp = write_one(
        client, cwid="cw-et1", content="x", session_id="s1",
        event_time="2026-11-11",
    )
    assert resp.status_code == 422
    resp = write_one(
        client, cwid="cw-et2", content="x", session_id="s1",
        event_time_precision="day",
    )
    assert resp.status_code == 422


def test_event_time_format_per_precision(client):
    ok_cases = [
        ("2026", "year"),
        ("2026-11", "month"),
        ("2026-11-11", "day"),
        ("2026-11-11", "instant"),
    ]
    for i, (et, prec) in enumerate(ok_cases):
        resp = write_one(
            client, cwid=f"cw-ok{i}", content="x", session_id="s1",
            event_time=et, event_time_precision=prec,
        )
        assert resp.status_code == 200, resp.text
    bad_cases = [
        ("2026-11", "year"),
        ("2026-13", "month"),
        ("2026-11-1", "day"),
        ("2026-02-30", "day"),
        ("2026-11-11T00:00:00Z", "instant"),
    ]
    for i, (et, prec) in enumerate(bad_cases):
        resp = write_one(
            client, cwid=f"cw-bad{i}", content="x", session_id="s1",
            event_time=et, event_time_precision=prec,
        )
        assert resp.status_code == 422, (et, prec, resp.text)


# ---- 并发 seq --------------------------------------------------------------


def test_concurrent_writes_same_session_seq_strictly_continuous(client):
    n = 20
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [
            pool.submit(
                write_one, client,
                cwid=f"cw-conc-{i}", content=f"msg-{i}", session_id="conc-sess",
            )
            for i in range(n)
        ]
        results = [f.result() for f in futures]
    assert all(r.status_code == 200 for r in results), [r.text for r in results]
    seqs = sorted(r.json()["seq"] for r in results)
    assert seqs == list(range(n))  # 严格连续、不撞号
    # 幂等键唯一性下无重复 id
    ids = [r.json()["id"] for r in results]
    assert len(set(ids)) == n


def test_concurrent_same_client_write_id_single_row_same_receipt(client, db):
    """并发幂等：两个线程同一 client_write_id 同时写，只落一条，都拿到同一回执。"""
    barrier = threading.Barrier(2)

    def call():
        barrier.wait(timeout=10)
        return write_one(
            client, cwid="cw-race-idem", content="same-body", session_id="s-race"
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = [f.result() for f in [pool.submit(call) for _ in range(2)]]

    assert all(r.status_code == 200 for r in results), [r.text for r in results]
    bodies = [r.json() for r in results]
    assert bodies[0]["id"] == bodies[1]["id"]
    assert bodies[0]["seq"] == bodies[1]["seq"]
    assert bodies[0]["recorded_at"] == bodies[1]["recorded_at"]
    assert sorted(bool(b.get("duplicated")) for b in bodies) == [False, True]

    rows = db.read_conn.execute(
        "SELECT COUNT(*) FROM memories WHERE client_write_id='cw-race-idem'"
    ).fetchone()[0]
    assert rows == 1


# ---- supersedes -------------------------------------------------------------


def _post_two(client):
    a = write_one(client, cwid="cw-sup-a", content="old", session_id="s1").json()
    b = write_one(client, cwid="cw-sup-b", content="new", session_id="s1").json()
    return a, b


def _edge(client, from_id, to_id, kind, reason=None):
    return client.post(
        "/supersedes",
        json={
            "from_id": from_id,
            "to_id": to_id,
            "kind": kind,
            "reason": reason,
            "source_client": "test-client",
        },
    )


def test_supersede_marks_voided_and_revoke_restores(client):
    a, b = _post_two(client)
    assert _edge(client, b["id"], a["id"], "supersede", reason="以新替旧").status_code == 200
    assert client.get(f"/memories/{a['id']}").json()["superseded"] is True
    # 撤销作废也走追加：更新的 revoke 边使其恢复
    assert _edge(client, b["id"], a["id"], "revoke").status_code == 200
    assert client.get(f"/memories/{a['id']}").json()["superseded"] is False
    # 再追加 supersede 又作废
    assert _edge(client, b["id"], a["id"], "supersede").status_code == 200
    assert client.get(f"/memories/{a['id']}").json()["superseded"] is True


def test_supersede_latest_edge_wins(client):
    a, b = _post_two(client)
    _edge(client, b["id"], a["id"], "supersede")
    _edge(client, b["id"], a["id"], "revoke")
    state = client.get("/supersedes/state", params={"from_id": b["id"], "to_id": a["id"]})
    assert state.status_code == 200
    assert state.json()["kind"] == "revoke"  # 最新边


def test_supersede_validations(client):
    a, b = _post_two(client)
    assert _edge(client, a["id"], b["id"], "wrong-kind").status_code == 422
    assert _edge(client, a["id"], "no-such-id", "supersede").status_code == 422


def test_same_millisecond_supersede_revoke_supersede(client, db, monkeypatch):
    """同毫秒追加三条边：旧三列主键会撞 PK，现应为自增 id 主键且最终状态正确。"""
    import app.write_path as write_path_mod

    a, b = _post_two(client)
    fixed_ms = 1_900_000_000_000
    monkeypatch.setattr(write_path_mod, "now_ms", lambda: fixed_ms)

    assert _edge(client, b["id"], a["id"], "supersede").status_code == 200
    assert _edge(client, b["id"], a["id"], "revoke").status_code == 200
    assert _edge(client, b["id"], a["id"], "supersede").status_code == 200

    assert client.get(f"/memories/{a['id']}").json()["superseded"] is True
    state = client.get(
        "/supersedes/state", params={"from_id": b["id"], "to_id": a["id"]}
    )
    assert state.status_code == 200
    assert state.json()["kind"] == "supersede"

    rows = db.read_conn.execute(
        "SELECT kind, recorded_at FROM supersedes ORDER BY id"
    ).fetchall()
    assert [r["kind"] for r in rows] == ["supersede", "revoke", "supersede"]
    assert {r["recorded_at"] for r in rows} == {fixed_ms}


def test_revoke_only_applies_to_its_own_pair(client):
    """逐 pair 语义：C 对 A 的 revoke 不能撤销 B 对 A 的 supersede。"""
    a, b = _post_two(client)
    c = write_one(client, cwid="cw-sup-c", content="third", session_id="s1").json()

    assert _edge(client, b["id"], a["id"], "supersede").status_code == 200
    assert _edge(client, c["id"], a["id"], "revoke").status_code == 200
    assert client.get(f"/memories/{a['id']}").json()["superseded"] is True

    # B 自己的 revoke 才恢复 A
    assert _edge(client, b["id"], a["id"], "revoke").status_code == 200
    assert client.get(f"/memories/{a['id']}").json()["superseded"] is False


def test_supersede_integrity_error_is_friendly_and_hides_sqlite_text(db, monkeypatch):
    import contextlib

    from app.write_path import record_supersede

    class FakeCursor:
        def fetchone(self):
            return (1,)

    class FakeConn:
        def execute(self, sql, params=()):
            if sql.lstrip().startswith("INSERT INTO supersedes"):
                raise sqlite3.IntegrityError(
                    "UNIQUE constraint failed: supersedes.from_id, supersedes.to_id"
                )
            return FakeCursor()

    @contextlib.contextmanager
    def fake_txn():
        yield FakeConn()

    monkeypatch.setattr(db, "write_txn", fake_txn)
    with pytest.raises(HTTPException) as excinfo:
        record_supersede(
            db, from_id="a", to_id="b", kind="supersede", source_client="t"
        )
    assert excinfo.value.status_code == 409
    assert "UNIQUE constraint" not in excinfo.value.detail
    assert "supersedes.from_id" not in excinfo.value.detail
