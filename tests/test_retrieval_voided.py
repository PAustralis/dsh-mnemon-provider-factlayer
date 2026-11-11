"""作废：per-pair 最新边（只按 id）、hidden 原始名次/出口替换、展开不复活。"""

from app import voided as voided_mod
from app.read_path import is_voided

from tests.retrieval_helpers import (
    add_edge,
    add_memory,
    add_session,
    build_derived,
    make_service,
)


def test_revoke_restores_record_per_pair(db):
    a = add_memory(db, "我的车是蓝色", session_id="s1")
    b = add_memory(db, "其实是白色", session_id="s2")
    add_edge(db, b, a, kind="supersede", recorded_at=100)
    build_derived(db)

    assert is_voided(db, a) is True
    service = make_service(db, routes=("bigram",))
    ids = [item["id"] for item in service.search("蓝色")["items"]]
    assert a not in ids and b in ids  # 路内替换：存活纠正者占 A 的名次

    add_edge(db, b, a, kind="revoke", recorded_at=200)
    assert is_voided(db, a) is False
    assert a in [item["id"] for item in service.search("蓝色")["items"]]


def test_revoke_only_affects_its_own_pair(db):
    a = add_memory(db, "蓝色记录", session_id="s1")
    b = add_memory(db, "纠正者 B", session_id="s2")
    c = add_memory(db, "纠正者 C", session_id="s3")
    add_edge(db, b, a, kind="supersede", recorded_at=100)
    add_edge(db, c, a, kind="supersede", recorded_at=110)
    add_edge(db, c, a, kind="revoke", recorded_at=120)
    assert is_voided(db, a) is True
    assert voided_mod.is_voided_id(db.read_conn, a) is True


def test_same_millisecond_revoke_then_supersede_is_ordered_by_id(db):
    a = add_memory(db, "同毫秒记录", session_id="s1")
    b = add_memory(db, "纠正者", session_id="s2")
    add_edge(db, b, a, kind="revoke", recorded_at=500)
    add_edge(db, b, a, kind="supersede", recorded_at=500)  # 后插入 id 更大 → 最新=supersede
    assert is_voided(db, a) is True

    c = add_memory(db, "待恢复记录", session_id="s4")
    add_edge(db, b, c, kind="supersede", recorded_at=500)
    add_edge(db, b, c, kind="revoke", recorded_at=500)  # 后插入 revoke → 最新=revoke
    assert is_voided(db, c) is False


def test_raw_route_includes_hidden_but_output_replaces(db):
    """v8 §4.2：各路取含 hidden 的原始 top-K；hidden 只在路内替换/出口处理。"""
    from tests.fake_embedding import HashingEmbeddingProvider

    provider = HashingEmbeddingProvider()
    target = add_memory(db, "北京项目计划书", session_id="s1")
    others = [add_memory(db, f"北京其他记录 {i}", session_id=f"s{i}") for i in range(3)]
    corrector = add_memory(db, "计划书改期了", session_id="s9")
    add_edge(db, corrector, target, kind="supersede", recorded_at=100)
    build_derived(db, embedding_provider=provider)

    service = make_service(
        db, routes=("vector", "bigram", "trigram"), provider=provider
    )
    # 原始 top-K 必须包含被作废项（不得在 SQL/mask 阶段排除）
    bigram_raw = service.bigram_route("北京项目计划书", 50).raw_ids
    assert target in bigram_raw
    vector_raw = service.vector_route("北京项目计划书", 50).raw_ids
    assert target in vector_raw

    result = service.search("北京项目计划书")
    ids = [item["id"] for item in result["items"]]
    assert target not in ids
    assert corrector in ids
    item = next(i for i in result["items"] if i["id"] == corrector)
    assert f"superseded-by:{target}" in item["tags"]
    assert item["score"] > 0  # 继承名次后参与 RRF 计分（不是旧 R5 的 score=None）
    # 未作废的同类记录仍在候选池
    assert any(mid in bigram_raw for mid in others)


def test_hidden_count_appended_to_first_content_and_tag(db):
    target = add_memory(db, "北京项目计划书", session_id="s1")
    corrector = add_memory(db, "计划书改期了", session_id="s9")
    add_edge(db, corrector, target, kind="supersede", recorded_at=100)
    build_derived(db)
    result = make_service(db, routes=("bigram",)).search("北京项目计划书")
    assert result["hidden"]["matched"] == 1
    assert "hidden:1" in result["items"][0]["tags"]
    assert "另有 1 条相关记录已被纠正" in result["items"][0]["content"]


def test_session_expansion_default_off(db):
    parent, neighbor, _alive = add_session(
        db, ["北京出差计划", "同一会话的邻域记录", "另一条邻域"], session_id="session-x"
    )
    build_derived(db)
    service = make_service(db, routes=("bigram",))
    assert service.cfg.expand_enabled is False
    ids = [item["id"] for item in service.search("北京出差")["items"]]
    assert parent in ids
    assert neighbor not in ids


def test_session_expansion_on_uses_replacement_and_stays_within_final_k(db):
    parent, neighbor = add_session(
        db, ["北京出差计划", "会被作废的邻域"], session_id="session-y"
    )
    corrector = add_memory(db, "邻域的正确版本", session_id="other")
    add_edge(db, corrector, neighbor, kind="supersede", recorded_at=100)
    build_derived(db)
    service = make_service(
        db, routes=("bigram",), expand_enabled=True, expand_window=1
    )
    result = service.search("北京出差")
    ids = [item["id"] for item in result["items"]]
    assert parent in ids
    assert corrector in ids  # 展开候选也过替换语义
    assert len(ids) <= 4  # 只能占用 FINAL_K 名额内
    expanded = next(i for i in result["items"] if i["id"] == corrector)
    assert "expanded" in expanded["tags"]
    assert expanded["inherited_rank"] == 1


def test_voided_helpers_agree_on_multi_edge_pairs(db):
    a = add_memory(db, "A", session_id="s1")
    b = add_memory(db, "B", session_id="s2")
    c = add_memory(db, "C", session_id="s3")
    add_edge(db, b, a, kind="supersede", recorded_at=100)
    add_edge(db, b, a, kind="revoke", recorded_at=200)
    add_edge(db, c, a, kind="supersede", recorded_at=150)
    assert is_voided(db, a) is True  # (C,A) 最新边仍是 supersede
    assert voided_mod.is_voided_id(db.read_conn, a) is True
    add_edge(db, c, a, kind="revoke", recorded_at=250)
    assert is_voided(db, a) is False
    assert voided_mod.is_voided_id(db.read_conn, a) is False
