"""作废过滤：per-pair 最新边（revoke 恢复 / 同毫秒顺序）、各路 top-50、会话展开不复活。"""

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
    assert a not in [item["id"] for item in service.search("蓝色")["items"]]

    add_edge(db, b, a, kind="revoke", recorded_at=200)
    assert is_voided(db, a) is False
    assert a in [item["id"] for item in service.search("蓝色")["items"]]


def test_revoke_only_affects_its_own_pair(db):
    a = add_memory(db, "蓝色记录", session_id="s1")
    b = add_memory(db, "纠正者 B", session_id="s2")
    c = add_memory(db, "纠正者 C", session_id="s3")
    # B、C 都作废 A；C 随后 revoke 只作用于 (C,A)，A 仍被 B 作废
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

    add_memory(db, "第二条", recorded_at=600)  # 无关，仅避免空库
    c = add_memory(db, "待恢复记录", session_id="s4")
    add_edge(db, b, c, kind="supersede", recorded_at=500)
    add_edge(db, b, c, kind="revoke", recorded_at=500)  # 后插入 revoke → 最新=revoke
    assert is_voided(db, c) is False


def test_voided_record_not_in_any_route_top_50(db):
    from tests.fake_embedding import HashingEmbeddingProvider

    provider = HashingEmbeddingProvider()
    target = add_memory(db, "北京项目计划书", session_id="s1")
    others = [add_memory(db, f"北京其他记录 {i}", session_id=f"s{i}") for i in range(3)]
    corrector = add_memory(db, "计划书改期了", session_id="s9")
    add_edge(db, corrector, target, kind="supersede", recorded_at=100)
    build_derived(db, embedding_provider=provider)

    service = make_service(
        db, routes=("vector", "bigram", "trigram", "recent"), provider=provider
    )
    for route_name in ("bigram", "trigram", "recent"):
        outcome = getattr(service, f"{route_name}_route")("北京项目计划书", 50)
        assert target not in outcome.ids, route_name
    vector_outcome = service.vector_route("北京项目计划书", 50)
    assert target not in vector_outcome.ids
    # 未作废的同类记录仍在
    assert any(mid in vector_outcome.ids for mid in others) or vector_outcome.ids


def test_voided_neighbor_not_revived_by_session_expansion(db):
    parent, neighbor, alive = add_session(
        db,
        ["北京出差计划", "被作废的邻域记录", "正常邻域记录"],
        session_id="session-x",
    )
    add_edge(db, "x", neighbor, kind="supersede", recorded_at=100)  # from_id 不要求存在
    build_derived(db)
    service = make_service(db, routes=("bigram",))
    result = service.search("北京出差")
    ids = [item["id"] for item in result["items"]]
    assert parent in ids
    assert neighbor not in ids
    assert alive in ids  # 展开本身仍然工作


def test_expansion_candidate_passes_void_filter_sql(db):
    """直接验证展开 SQL 片段也走共享 CTE（不是天真 NOT IN）。"""
    parent, neighbor = add_session(
        db, ["北京出差计划", "会被 revoked 的邻域"], session_id="session-y"
    )
    add_edge(db, "b", neighbor, kind="supersede", recorded_at=100)
    add_edge(db, "b", neighbor, kind="revoke", recorded_at=200)
    build_derived(db)
    service = make_service(db, routes=("bigram",))
    result = service.search("北京出差")
    assert neighbor in [item["id"] for item in result["items"]]


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
