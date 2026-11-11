"""v8 路线语义：无单字回退 / NO_MATCH / trigram 可选 / 路线开关 / 图路降级。"""

import pytest

from tests.fake_embedding import HashingEmbeddingProvider
from tests.retrieval_helpers import (
    add_memory,
    build_derived,
    make_service,
)


def test_phrase_zero_hit_does_not_fall_back_to_single_chars(db):
    """基线语义：短语 0 命中就是 NO_MATCH，不构造单字兜底（2/40 空结果来源）。"""
    add_memory(db, "甲方违约需要赔偿", session_id="s1")
    build_derived(db)
    service = make_service(db, routes=("bigram",))
    result = service.search("丙方")
    assert result["items"] == []
    assert result["hint"].startswith("NO_MATCH")
    assert "routes=bigram(ok)" in result["hint"]
    assert "tokens=1" in result["hint"]


def test_short_query_returns_no_match_without_error(db):
    add_memory(db, "喝茶", session_id="s1")
    build_derived(db)
    service = make_service(db, routes=("bigram",))
    result = service.search("茶")
    assert result["items"] == []
    assert result["hint"].startswith("NO_MATCH")


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


def test_trigram_default_off_per_v8(db, monkeypatch):
    from app.config import load_config

    monkeypatch.delenv("RETRIEVAL_ROUTES", raising=False)
    monkeypatch.delenv("DSH_RETRIEVAL_ROUTES", raising=False)
    cfg = load_config()
    assert cfg.route_names == ("bigram",)


def test_recent_fallback_is_deleted(db):
    """v8 删除“最近分区”：命中不足 FINAL_K 也不补最近项。"""
    add_memory(db, "唯一命中北京的记录", session_id="s1")
    for i in range(8):
        add_memory(db, f"无关记录 {i}", session_id=f"other-{i}")
    build_derived(db)
    service = make_service(db, routes=("bigram",))
    result = service.search("北京")
    assert "recent" not in result["routes"]
    assert "fallback" not in result
    assert len(result["items"]) == 1


def test_no_recent_padding_when_query_has_no_hits(db):
    add_memory(db, "旧记录", session_id="a")
    build_derived(db)
    service = make_service(db, routes=("bigram",))
    result = service.search("完全不存在的词")
    assert result["items"] == []
    assert result["hint"].startswith("NO_MATCH")


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
    assert result["items"]


def test_index_is_single_mixed_column_baseline_tokens(db):
    add_memory(db, "索引体积检查：中文单字与双字都进 ngrams", session_id="s1")
    build_derived(db)
    row = db.read_conn.execute("SELECT ngram_text FROM memories_ngrams").fetchone()
    tokens = row["ngram_text"].split(" ")
    content = "索引体积检查：中文单字与双字都进 ngrams"
    # 单混合列：CJK 只产双字，Latin 整段一个 token；token 数远小于原文长度
    assert tokens
    assert len(tokens) <= len(content)
    # 不产单字（除非某个 CJK 段长度恰为 1）
    assert "索" not in tokens
    assert "索引" in tokens


def test_bigram_fts_is_one_mixed_column(db):
    cols = [
        row["name"]
        for row in db.read_conn.execute(
            "PRAGMA table_info(memories_fts_bigram)"
        ).fetchall()
    ]
    assert cols == ["ngram_text"]  # 不拆 uni/bi 两列
    # 索引侧与查询侧共用同一函数
    from app import textnorm

    assert textnorm.ngram_tokens("我爱北京") == textnorm.bigrams_of("我爱北京")


def test_write_then_search_immediately(db):
    """v8 §9.10：写入成功立即可检索（ngram 派生与事实写入同一事务）。"""
    from app.write_path import create_memory

    memory_id = create_memory(
        db,
        client_write_id="cw-immediate",
        content="写入之后马上应该能搜到北京记录",
        session_id="s-immediate",
        source_client="t",
    )["id"]
    service = make_service(db, routes=("bigram",))
    result = service.search("北京记录")
    assert memory_id in [item["id"] for item in result["items"]]
