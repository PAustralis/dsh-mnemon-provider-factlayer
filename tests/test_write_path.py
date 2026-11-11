"""写入路径：回执、幂等、409、event_time 校验、并发 seq 连续、supersedes 边。"""

import concurrent.futures

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
