"""R2 回退计数 / R3 trigram / R6 最近兜底 / 路线开关。"""

import pytest

from app.embeddings import EmbeddingModelMismatch

from tests.fake_embedding import HashingEmbeddingProvider
from tests.retrieval_helpers import add_memory, add_session, build_derived, make_service


def test_phrase_zero_hit_falls_back_to_single_chars_and_counts(db):
    # 短语 token「丙方」在库里不存在，但单字「方」命中 → 必须回退并被计数
    ids = [add_memory(db, "甲方违约需要赔偿", session_id="s1")]
    build_derived(db)
    service = make_service(db, routes=("bigram",))
    result = service.search("丙方")
    assert ids[0] in [item["id"] for item in result["items"]]
    assert result["fallback"]["phrase_to_or"] is True
    assert service.stats.phrase_fallback_count == 1
    assert result["routes"]["bigram"]["raw"] >= 1


def test_short_query_goes_directly_to_single_chars_without_fallback(db):
    ids = [add_memory(db, "喝茶", session_id="s1")]
    build_derived(db)
    service = make_service(db, routes=("bigram",))
    result = service.search("茶")
    assert ids[0] in [item["id"] for item in result["items"]]
    assert result["fallback"]["phrase_to_or"] is False
    assert service.stats.phrase_fallback_count == 0


def test_trigram_route_hits_three_char_query(db):
    ids = [add_memory(db, "我们明天去北京出差", session_id="s1")]
    build_derived(db)
    service = make_service(db, routes=("trigram",))
    result = service.search("明天去")
    assert ids[0] in [item["id"] for item in result["items"]]


def test_trigram_route_unavailable_for_short_query(db):
    add_memory(db, "我们明天去北京出差", session_id="s1")
    build_derived(db)
    service = make_service(db, routes=("trigram",))
    result = service.search("明天")
    assert result["routes"]["trigram"]["skipped"] == "query_shorter_than_3_chars"
    assert service.stats.snapshot()["routes"]["trigram"]["empty_rate"] == 1.0


def test_trigram_default_off_per_v31(db, monkeypatch):
    from app.config import load_config

    monkeypatch.delenv("RETRIEVAL_ROUTES", raising=False)
    monkeypatch.delenv("DSH_RETRIEVAL_ROUTES", raising=False)
    cfg = load_config()
    assert "trigram" not in cfg.route_names
    assert "bigram" in cfg.route_names


def test_recent_fallback_only_when_fewer_than_final_k(db):
    # 1 条命中 < FINAL_K=6 → 触发兜底
    add_memory(db, "唯一命中北京的记录", session_id="s1")
    for i in range(8):
        add_memory(db, f"无关记录 {i}", session_id=f"other-{i}")
    build_derived(db)
    service = make_service(db, routes=("bigram",))
    result = service.search("北京")
    assert result["fallback"]["recent"] is True
    assert "recent" in result["routes"]


def test_recent_fallback_not_triggered_when_enough_candidates(db):
    ids = [add_memory(db, f"北京记录 {i}", session_id=f"s{i}") for i in range(7)]
    build_derived(db)
    service = make_service(db, routes=("bigram",))
    result = service.search("北京")
    assert result["fallback"]["recent"] is False
    assert "recent" not in result["routes"]
    assert len(result["items"]) == 6
    assert {item["id"] for item in result["items"]} <= set(ids)


def test_recent_fallback_orders_by_recorded_at_desc(db):
    from tests.retrieval_helpers import insert_memory

    insert_memory(db, "old", "最近记录 old", session_id="a", seq=0, recorded_at=1000)
    insert_memory(db, "new", "最近记录 new", session_id="b", seq=0, recorded_at=2000)
    build_derived(db)
    service = make_service(db, routes=("bigram",))
    result = service.search("完全不存在的词")
    assert result["fallback"]["recent"] is True
    assert [item["id"] for item in result["items"]][:2] == ["new", "old"]


@pytest.mark.parametrize("route", ["vector", "bigram", "trigram", "graph"])
def test_each_route_can_be_disabled_without_error(db, route):
    add_memory(db, "北京记录", session_id="s1")
    build_derived(db)
    provider = HashingEmbeddingProvider() if route == "vector" else None
    if provider is not None:
        build_derived(db, embedding_provider=provider)
    service = make_service(db, routes=(route,), provider=provider)
    result = service.search("北京")
    assert isinstance(result["items"], list)
    assert set(result["routes"]) >= {route}


def test_graph_route_returns_empty_and_counts_unavailable(db):
    add_memory(db, "北京记录", session_id="s1")
    build_derived(db)
    service = make_service(db, routes=("graph", "bigram"))
    result = service.search("北京")
    assert result["routes"]["graph"]["unavailable"] is True
    assert result["routes"]["graph"]["skipped"] == "graph_not_implemented"
    snapshot = service.stats.snapshot()
    assert snapshot["routes"]["graph"]["unavailable"] == 1
    # 其余路照常出结果，不因 graph 未实现而整体失败
    assert result["items"]


def test_vector_route_skips_when_index_empty(db):
    from tests.fake_embedding import FailingEmbeddingProvider

    add_memory(db, "北京记录", session_id="s1")
    build_derived(db)
    provider = FailingEmbeddingProvider()
    service = make_service(db, routes=("vector", "bigram"), provider=provider)
    result = service.search("北京")
    assert result["routes"]["vector"]["skipped"] == "empty_embedding_index"
    assert provider.calls == 0  # 空索引不花钱调 API
    assert result["items"]  # bigram 照常


def test_index_size_sanity_matches_design(db):
    add_memory(db, "索引体积检查：中文单字与双字都进 ngrams", session_id="s1")
    build_derived(db)
    row = db.read_conn.execute("SELECT ngram_text FROM memories_ngrams").fetchone()
    tokens = row["ngram_text"].split(" ")
    content_len = len("索引体积检查：中文单字与双字都进 ngrams")
    # 索引 token 数不超过原文 2–3 倍的量级（设计 §3.1）
    assert tokens and len(tokens) <= content_len * 3
