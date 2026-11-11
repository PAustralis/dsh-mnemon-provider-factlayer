"""v8 §5 / 03 §6 契约输出：score、createdAt、tags 通道、NO_MATCH、预算、admin 端点。"""

from __future__ import annotations

import json
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
