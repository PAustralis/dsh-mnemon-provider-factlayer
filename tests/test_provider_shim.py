"""对接层 shim 侧（docs/06 v3）：GET /spaces、O(1) /health、重建期 503、
以及 ``/search`` 不按 session_id 过滤。

这些测试刻意**不依赖后台线程的时序**：
- 需要新快照就显式 ``health_cache.refresh_now()``（``prime_health``）；
- 「缓存未就绪」用 monkeypatch 掉 ``start()``（不 prime）来构造；
- 重建期用 ``app.state.rebuild_gate`` 直接置位，或用 monkeypatch 的
  ``pipeline.rebuild_all`` 观察标志生命周期。
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.health_cache import HealthCache
from app.main import create_app
from app.rebuild_gate import REBUILDING_MESSAGE, RebuildGate
from tests.conftest import prime_health, write_one


def _hermetic_env(monkeypatch) -> None:
    """与 conftest.client 相同的 hermetic 环境（不联网、不起后台派生循环）。"""
    monkeypatch.setenv("DSH_DERIVE_LOOP_ENABLED", "0")
    monkeypatch.setenv("DSH_BACKUP_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("RETRIEVAL_ROUTES", "bigram")
    for name in (
        "EMBEDDING_MODEL",
        "DSH_EMBEDDING_MODEL",
        "EMBEDDING_BASE_URL",
        "DSH_EMBEDDING_BASE_URL",
        "DSH_EMBEDDING_API_URL",
        "EMBEDDING_API_KEY",
        "DSH_EMBEDDING_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------- /spaces


def test_spaces_returns_single_fixed_space(client):
    response = client.get("/spaces")
    assert response.status_code == 200
    assert response.json() == [{"id": "factlayer-default", "name": "factlayer"}]


def test_spaces_reads_config_only_not_the_database(client, db, monkeypatch):
    """O(1)：不查库、不改库——把读连接改成炸掉也必须照常返回。"""
    # 后台快照线程会读库，先停掉，保证「炸掉的 read_conn」只可能来自 /spaces。
    client.app.state.health_cache.stop()

    class _NoDb:
        @property
        def read_conn(self):  # pragma: no cover - 被调用即失败
            raise AssertionError("/spaces 不得访问数据库")

    monkeypatch.setattr(type(db), "read_conn", _NoDb.read_conn)
    response = client.get("/spaces")
    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == ["factlayer-default"]


def test_spaces_honours_factlayer_space_id_env(db_path, monkeypatch):
    _hermetic_env(monkeypatch)
    monkeypatch.setenv("FACTLAYER_SPACE_ID", "team-a")
    app = create_app(db_path=db_path)
    with TestClient(app) as client:
        assert client.get("/spaces").json() == [{"id": "team-a", "name": "factlayer"}]


# ---------------------------------------------------------------- /health O(1)


def test_health_cache_unit_not_ready_then_ready():
    cache = HealthCache(lambda: {"pending": {"fts": 0}}, interval_seconds=0.05)
    try:
        assert cache.snapshot() is None
        cache.start(prime=False)
        assert cache.snapshot() is None  # 线程刚起，还没刷出第一份
        deadline = time.time() + 5
        while time.time() < deadline and cache.snapshot() is None:
            time.sleep(0.02)
        snapshot = cache.snapshot()
        assert snapshot is not None
        assert snapshot["status"] == "ready"
        assert snapshot["pending"] == {"fts": 0}
        assert snapshot["cache"]["refresh_interval_seconds"] == pytest.approx(0.05)
    finally:
        cache.stop()


def test_health_cache_keeps_last_snapshot_when_compute_fails():
    calls = {"n": 0}

    def compute() -> dict:
        calls["n"] += 1
        if calls["n"] == 1:
            return {"pending": {"fts": 0}}
        raise RuntimeError("boom")

    cache = HealthCache(compute, interval_seconds=60)
    cache.refresh_now()
    assert cache.snapshot()["pending"] == {"fts": 0}
    assert cache.refresh_now() is None
    snapshot = cache.snapshot()
    assert snapshot["pending"] == {"fts": 0}  # 旧快照保留
    assert "RuntimeError" in snapshot["cache"]["last_error"]


def test_health_rebuilding_flag_is_read_from_gate_not_snapshot():
    gate = RebuildGate()
    cache = HealthCache(lambda: {"pending": {"fts": 0}}, rebuilding=gate)
    cache.refresh_now()
    assert cache.snapshot()["rebuilding"] is False
    gate.begin()
    try:
        assert cache.snapshot()["rebuilding"] is True  # 无需重算
    finally:
        gate.end()
    assert cache.snapshot()["rebuilding"] is False


def test_health_returns_starting_before_first_snapshot(db_path, monkeypatch):
    """``starting`` 只出现在「快照尚未就绪」时，不是常态。

    真实部署里 ``lifespan`` 调 ``health_cache.start()``，默认 ``prime=True``
    **同步**算出一份快照后才启动刷新线程，所以正常启动的进程第一次
    /health 就是 ready。``starting`` 主要出现在首轮 ``_compute_health`` 失败
    （库锁、迁移未完成等）导致 ``refresh_now()`` 返回 None 时；本测试把
    ``start()`` 换掉（压根不 prime）是为了稳定复现「缓存未就绪」这个分支，
    不依赖后台线程时序。
    """
    _hermetic_env(monkeypatch)
    app = create_app(db_path=db_path)
    cache = app.state.health_cache
    monkeypatch.setattr(cache, "start", lambda **kwargs: None)  # 不 prime
    monkeypatch.setattr(cache, "stop", lambda **kwargs: None)
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "starting"}


def test_health_is_served_from_cache_without_recomputing(client, db, monkeypatch):
    write_one(client, cwid="cw-cache-1", content="缓存不必重算", session_id="s-cache")
    before = prime_health(client)
    assert before["pending"]["embedding"] == 1

    client.app.state.health_cache.stop()
    import app.main as main_mod

    def boom(*args, **kwargs):
        raise AssertionError("/health 请求路径不得重跑聚合")

    monkeypatch.setattr(main_mod, "health_report", boom)
    after = client.get("/health")
    assert after.status_code == 200
    body = after.json()
    assert body["pending"] == before["pending"]
    assert body["status"] == "ready"
    assert body["cache"]["age_ms"] >= 0
    assert body["cache"]["generated_at_ms"] == before["cache"]["generated_at_ms"]


# ---------------------------------------------------------------- 重建期


def _assert_rebuilding_503(response) -> None:
    assert response.status_code == 503
    assert response.json() == {"error": "rebuilding", "message": REBUILDING_MESSAGE}


def test_rebuild_gate_counts_overlapping_rebuilds():
    """两个重建重叠时，先结束的那个不得提前解除 503。"""
    gate = RebuildGate()
    gate.begin()
    gate.begin()
    gate.end()
    assert gate.is_rebuilding is True
    gate.end()
    assert gate.is_rebuilding is False
    # 错误配对也不把计数打到负数
    gate.end()
    assert gate.is_rebuilding is False


def test_rebuilding_blocks_search_and_memories_with_explicit_503(client, db):
    write_one(client, cwid="cw-rb-a", content="重建前写入", session_id="s-rb")
    gate = client.app.state.rebuild_gate
    gate.begin()
    try:
        _assert_rebuilding_503(client.post("/search", json={"query": "重建"}))
        _assert_rebuilding_503(client.get("/memories", params={"session_id": "s-rb"}))
        _assert_rebuilding_503(
            client.post(
                "/memories",
                json={
                    "client_write_id": "cw-rb-blocked",
                    "content": "重建期间不得写入",
                    "session_id": "s-rb",
                    "source_client": "t",
                },
            )
        )
        # 写入确实没发生（不是静默吞掉）
        assert (
            db.read_conn.execute(
                "SELECT COUNT(*) FROM memories WHERE client_write_id='cw-rb-blocked'"
            ).fetchone()[0]
            == 0
        )
        # /health 仍 O(1) 可读，且 rebuilding 现读为 true；discover 不受影响
        assert client.get("/health").json()["rebuilding"] is True
        assert client.get("/spaces").status_code == 200
    finally:
        gate.end()
    # 解除后恢复服务（旧索引完整可用，不是半张）
    assert client.post("/search", json={"query": "重建前写入"}).status_code == 200


def test_rebuilding_blocks_admin_search_and_supersedes(client, db):
    """重建期 ``/admin/search`` 与 ``/supersedes`` 也必须 503（第三层审查 #2）。

    ``/supersedes`` 选方案 (a)（直接 503）：写边若落在「重建读快照」之后，派生
    层会少算一次作废，产生「边已写、检索却按旧图作答」的派生不一致。与
    ``/memories`` 保持同一语义（明确 503、客户端可重试），比方案 (b)「先写边
    再标 rebuild_in_progress、依赖后续 derive 补齐」少一个静默不一致窗口。
    """
    first = write_one(
        client, cwid="cw-rb-edge-a", content="旧结论", session_id="s-rb"
    ).json()
    second = write_one(
        client, cwid="cw-rb-edge-b", content="新结论", session_id="s-rb"
    ).json()
    payload = {
        "from_id": first["id"],
        "to_id": second["id"],
        "kind": "supersede",
        "source_client": "test-client",
    }
    gate = client.app.state.rebuild_gate
    gate.begin()
    try:
        _assert_rebuilding_503(client.get("/admin/search", params={"query": "结论"}))
        _assert_rebuilding_503(client.post("/supersedes", json=payload))
        # 写边确实没落盘（不是静默吞掉）
        assert (
            db.read_conn.execute("SELECT COUNT(*) FROM supersedes").fetchone()[0] == 0
        )
    finally:
        gate.end()
    # 解除后两者恢复：admin 检索 200，写边正常追加
    assert client.get("/admin/search", params={"query": "结论"}).status_code == 200
    assert client.post("/supersedes", json=payload).status_code == 200
    assert db.read_conn.execute("SELECT COUNT(*) FROM supersedes").fetchone()[0] == 1


def test_admin_rebuild_sets_flag_during_work_and_clears_after(client, monkeypatch):
    gate = client.app.state.rebuild_gate
    seen: dict[str, bool] = {}

    def fake_rebuild_all(**kwargs):
        seen["during"] = gate.is_rebuilding
        return {"fts": 0, "embedding": 0, "entity": 0, "graph": 0}

    monkeypatch.setattr(client.app.state.pipeline, "rebuild_all", fake_rebuild_all)
    response = client.post("/admin/rebuild")
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert seen["during"] is True
    assert gate.is_rebuilding is False


def test_admin_rebuild_clears_flag_after_failure(client, monkeypatch):
    gate = client.app.state.rebuild_gate

    def boom(*args, **kwargs):
        assert gate.is_rebuilding is True
        raise RuntimeError("simulated rebuild failure")

    monkeypatch.setattr(client.app.state.pipeline, "rebuild_all", boom)
    with pytest.raises(RuntimeError):
        client.post("/admin/rebuild")
    assert gate.is_rebuilding is False
    # 失败后读路径恢复（重建没留下半张索引；具体事务语义见 test_derive）
    assert client.post("/search", json={"query": "x"}).status_code == 200


# ---------------------------------------------------------------- session_id


def test_search_does_not_filter_by_session_id(client):
    """docs/06 §5：/search 不得按 session_id 过滤，否则两台机器会无声分裂。"""
    first = write_one(
        client, cwid="cw-sess-a", content="北京项目计划书 预算", session_id="sess-a"
    ).json()
    second = write_one(
        client, cwid="cw-sess-b", content="北京项目计划书 排期", session_id="sess-b"
    ).json()

    # 不带 session_id：两条不同 session 的记录都能被同一查询召回
    result = client.post("/search", json={"query": "北京项目计划书"}).json()
    ids = [item["id"] for item in result["items"]]
    assert first["id"] in ids
    assert second["id"] in ids

    # 即使显式传了 session_id（契约里没有这个概念），也不得据此过滤
    filtered = client.post(
        "/search", json={"query": "北京项目计划书", "session_id": "sess-a"}
    ).json()
    filtered_ids = [item["id"] for item in filtered["items"]]
    assert first["id"] in filtered_ids
    assert second["id"] in filtered_ids
