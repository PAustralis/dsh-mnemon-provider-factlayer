"""v8 §5 / 03 §6 契约输出：score、createdAt、tags 通道、NO_MATCH、预算、admin 端点。"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import pytest

from app.main import create_app
from tests.fake_embedding import FailingEmbeddingProvider, HashingEmbeddingProvider
from tests.retrieval_helpers import (
    add_edge,
    add_memory,
    build_derived,
    make_service,
)


def _assert_contract_items(items):
    for item in items:
        assert isinstance(item["score"], (int, float))
        assert item["score"] > 0
        assert isinstance(item["createdAt"], str)
        parsed = datetime.fromisoformat(item["createdAt"].replace("Z", "+00:00"))
        assert parsed.tzinfo is not None
        assert parsed.utcoffset() == timezone.utc.utcoffset(None)
        assert isinstance(item["tags"], list)
        assert len(item["tags"]) <= 8
        assert all(len(tag) <= 300 for tag in item["tags"])
        assert all("," not in tag and "，" not in tag for tag in item["tags"])
        assert len(item["content"]) <= 1200


def test_adapter_manifest_declares_provider_native_score_semantics(client):
    health = client.get("/health").json()
    manifest = health["provider_manifest"]
    assert manifest["scoreSemantics"] == "provider-native"
    assert manifest["finalK"] == 4
    assert manifest["itemMaxChars"] == 1200
    assert manifest["totalMaxChars"] == 4800


def test_every_item_has_numeric_score_and_iso_utc_created_at(db):
    for i in range(6):
        add_memory(db, f"北京出差记录 {i}", session_id=f"s{i}")
    build_derived(db)
    service = make_service(db, routes=("bigram",))
    result = service.search("北京出差")
    assert result["items"]
    _assert_contract_items(result["items"])
    # adapter 声明 provider-native（RRF 顺序直接生效，不归一化）
    assert service.cfg.snapshot()["score_semantics"] == "provider-native"
    assert result["hint"] is None  # 结果非空时状态不走 hint
    # 每条都有来源路 tag（状态通道）
    for item in result["items"]:
        assert any(tag.startswith("via:") for tag in item["tags"])


def test_weak_tag_is_used_for_tail_ranked_item(db):
    for i in range(6):
        add_memory(db, f"北京出差记录 {i}", session_id=f"s{i}")
    build_derived(db)
    service = make_service(db, routes=("bigram",), weak_best_rank=3)
    result = service.search("北京出差")
    assert any("weak" in item["tags"] for item in result["items"])


def test_vector_unavailable_goes_to_tags_when_results_exist(db):
    provider = HashingEmbeddingProvider()
    add_memory(db, "北京出差计划", session_id="s1")
    build_derived(db, embedding_provider=provider)
    failing = FailingEmbeddingProvider()
    failing.model_id = provider.model_id
    service = make_service(db, routes=("vector", "bigram"), provider=failing)
    result = service.search("北京出差")
    assert result["items"]
    assert result["hint"] is None
    assert "vector-unavailable" in result["items"][0]["tags"]


def test_no_match_hint_starts_with_no_match_and_summarizes_routes(db):
    add_memory(db, "北京记录", session_id="s1")
    build_derived(db)
    service = make_service(db, routes=("bigram", "trigram"))
    result = service.search("完全不存在的词组")
    assert result["items"] == []
    hint = result["hint"]
    assert hint.startswith("NO_MATCH")
    assert "routes=" in hint
    assert "tokens=" in hint
    assert "hidden=" in hint
    assert result["count"] == 0


def test_empty_token_query_is_no_match(db):
    _ = add_memory(db, "北京记录", session_id="s1")
    build_derived(db)
    service = make_service(db, routes=("bigram",))
    result = service.search("，。！？")
    assert result["hint"].startswith("NO_MATCH")
    assert "tokens=0" in result["hint"]


def test_long_query_truncates_tokens_to_200_and_reports_count(db):
    _ = add_memory(db, "北京记录", session_id="s1")
    build_derived(db)
    service = make_service(db, routes=("bigram",))
    query = " ".join(f"token{i:03d}" for i in range(250))
    result = service.search(query)
    assert result["token_count"] == 200
    assert result["token_truncated"] == 50
    assert result["hint"].startswith("NO_MATCH")
    assert "+50" in result["hint"]


def test_item_and_total_character_budget(db):
    # 5 条超长命中：单条 ≤1200、总 ≤4800
    for i in range(5):
        add_memory(db, "北京" + ("很长的记录内容" * 400) + f" {i}", session_id=f"s{i}")
    build_derived(db)
    service = make_service(db, routes=("bigram",))
    result = service.search("北京")
    assert len(result["items"]) <= 4
    _assert_contract_items(result["items"])
    total = sum(len(item["content"]) for item in result["items"])
    assert total <= 4800
    assert result["output"]["chars"] <= 4800


def test_hidden_count_tag_and_first_content_tail(db):
    target = add_memory(db, "北京项目计划书", session_id="s1")
    corrector = add_memory(db, "计划书改期了", session_id="s9")
    add_edge(db, corrector, target, kind="supersede", recorded_at=100)
    build_derived(db)
    result = make_service(db, routes=("bigram",)).search("北京项目计划书")
    first = result["items"][0]
    assert "hidden:1" in first["tags"]
    assert "另有 1 条相关记录已被纠正" in first["content"]
    _assert_contract_items(result["items"])


def test_include_superseded_is_unreachable_from_model_search(db):
    target = add_memory(db, "北京项目计划书", session_id="s1")
    corrector = add_memory(db, "计划书改期了", session_id="s9")
    add_edge(db, corrector, target, kind="supersede", recorded_at=100)
    build_derived(db)
    service = make_service(db, routes=("bigram",))
    normal = service.search("北京项目计划书")
    assert target not in [item["id"] for item in normal["items"]]
    admin = service.search("北京项目计划书", include_superseded=True)
    assert target in [item["id"] for item in admin["items"]]


def test_exit_assertion_output_is_disjoint_from_hidden(db):
    target = add_memory(db, "北京项目计划书", session_id="s1")
    corrector = add_memory(db, "计划书改期了", session_id="s9")
    add_edge(db, corrector, target, kind="supersede", recorded_at=100)
    build_derived(db)
    service = make_service(db, routes=("bigram",))
    from app import voided as voided_mod

    hidden = voided_mod.hidden_id_set(db.read_conn)
    result = service.search("北京项目计划书")
    output = {item["id"] for item in result["items"]}
    assert output & hidden == set()
    assert service.stats.snapshot()["exit_assertion_drops"] == 0


def test_exit_assertion_catches_deliberate_hidden_leak(db, monkeypatch, caplog):
    """故意制造泄漏：hidden 映射到自己 → 路内替换不剔除，只有出口断言能拦。

    ``snapshot.heads`` 置空时 ``_apply_route_replacement`` 会把被作废项直接丢弃，
    到不了出口；因此这里把 heads[target] 指向 target 本身，模拟替换逻辑有 bug。
    """
    target = add_memory(db, "北京项目计划书", session_id="s1")
    add_memory(db, "北京项目计划书附件", session_id="s2")  # 剔除泄漏项后结果仍非空
    corrector = add_memory(db, "计划书改期了", session_id="s9")
    add_edge(db, corrector, target, kind="supersede", recorded_at=100)
    build_derived(db)

    from app import voided as voided_mod

    real_snapshot = voided_mod.hidden_snapshot

    def leaking_snapshot(conn):
        snapshot = real_snapshot(conn)
        snapshot.heads[target] = [target]
        return snapshot

    monkeypatch.setattr(voided_mod, "hidden_snapshot", leaking_snapshot)
    service = make_service(db, routes=("bigram",))
    caplog.set_level(logging.ERROR, logger="factlayer.retrieval")

    result = service.search("北京项目计划书")

    # 泄漏项被剔除、结果仍正常返回；丢弃计数 +1
    output_ids = [item["id"] for item in result["items"]]
    assert target not in output_ids
    assert output_ids, "泄漏项被剔除后仍应有其它命中（验证不是整条查询失败）"
    assert service.stats.snapshot()["exit_assertion_drops"] == 1
    # ERROR 日志带 query_id，便于复现
    errors = [rec for rec in caplog.records if rec.levelno == logging.ERROR]
    assert errors
    assert any(result["query_id"] in rec.getMessage() for rec in errors)
    assert any(
        target in rec.getMessage() and "hidden" in rec.getMessage() for rec in errors
    )


def test_first_item_two_tail_lines_still_within_item_max(db):
    """首条同时拼 hidden 尾行 + 预算尾行时，单条仍须 ≤1200（否则下游硬截掉通知）。"""
    for i in range(6):
        add_memory(db, "北京" + ("很长的记录内容" * 400) + f" {i}", session_id=f"s{i}")
    target = add_memory(
        db, "北京" + ("很长的记录内容" * 400) + " 被作废", session_id="sh"
    )
    corrector = add_memory(db, "改期了", session_id="sc")
    add_edge(db, corrector, target, kind="supersede", recorded_at=100)
    build_derived(db)

    # final_k=5 让第 5 条触发总预算截断 → 首条除 hidden 尾行外再叠一条预算尾行
    service = make_service(
        db, routes=("bigram",), final_k=5, item_max_chars=1200, total_max_chars=4800
    )
    result = service.search("北京")

    assert result["items"]
    first = result["items"][0]
    assert "hidden:1" in first["tags"]
    assert len(first["content"]) <= 1200  # 单条含**两条**尾行也不得超单条预算
    assert result["output"]["truncated_items"] >= 1
    assert "条因字符预算未显示" in first["content"]  # 尾行没有被 1200 硬截掉
    _assert_contract_items(result["items"])
    assert sum(len(item["content"]) for item in result["items"]) <= 4800


# ---- admin 端点 -------------------------------------------------------------


def test_admin_search_include_superseded_endpoint(client):
    a = client.post(
        "/memories",
        json={
            "client_write_id": "cw-adm-a",
            "content": "北京项目计划书",
            "session_id": "s1",
            "source_client": "t",
        },
    ).json()
    b = client.post(
        "/memories",
        json={
            "client_write_id": "cw-adm-b",
            "content": "计划书改期了",
            "session_id": "s2",
            "source_client": "t",
        },
    ).json()
    client.post(
        "/supersedes",
        json={
            "from_id": b["id"],
            "to_id": a["id"],
            "kind": "supersede",
            "source_client": "t",
        },
    )
    client.post("/derive/tick")
    normal = client.get("/admin/search", params={"query": "北京项目计划书"}).json()
    assert a["id"] not in [item["id"] for item in normal["items"]]
    admin = client.get(
        "/admin/search",
        params={"query": "北京项目计划书", "include_superseded": 1},
    ).json()
    assert a["id"] in [item["id"] for item in admin["items"]]
    # 模型路径 POST /search 没有 include_superseded 参数位
    model = client.post(
        "/search", json={"query": "北京项目计划书", "include_superseded": 1}
    ).json()
    assert a["id"] not in [item["id"] for item in model["items"]]


def test_admin_token_protects_admin_endpoints(db_path, monkeypatch):
    monkeypatch.setenv("DSH_DERIVE_LOOP_ENABLED", "0")
    monkeypatch.setenv("DSH_BACKUP_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("RETRIEVAL_ROUTES", "bigram")
    monkeypatch.setenv("ADMIN_TOKEN", "s3cret")
    from fastapi.testclient import TestClient

    app = create_app(db_path=db_path)
    with TestClient(app) as client:
        assert client.post("/admin/rebuild").status_code == 403
        assert client.get("/admin/search", params={"query": "x"}).status_code == 403
        assert (
            client.post(
                "/admin/rebuild", headers={"X-Admin-Token": "wrong"}
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/admin/rebuild", headers={"X-Admin-Token": "s3cret"}
            ).status_code
            == 200
        )
        assert (
            client.get(
                "/admin/search",
                params={"query": "x"},
                headers={"Authorization": "Bearer s3cret"},
            ).status_code
            == 200
        )


def test_admin_rebuild_is_one_transaction_and_keeps_facts(client, db):
    for i in range(3):
        client.post(
            "/memories",
            json={
                "client_write_id": f"cw-rb-{i}",
                "content": f"北京记录 {i}",
                "session_id": f"s{i}",
                "source_client": "t",
            },
        )
    before = db.read_conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    response = client.post("/admin/rebuild")
    assert response.status_code == 200
    assert response.json()["counts"]["fts"] == 3
    assert db.read_conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == before
    assert db.read_conn.execute("SELECT COUNT(*) FROM memories_ngrams").fetchone()[0] == 3
    assert (
        db.read_conn.execute("SELECT COUNT(*) FROM memories_fts_bigram").fetchone()[0]
        == 3
    )
    # 重建后立即可检索（查询看到完整新索引）
    result = client.post("/search", json={"query": "北京记录"}).json()
    assert result["items"]
