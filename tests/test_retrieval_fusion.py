"""RRF 融合：分数公式、gseq DESC tie-break、FINAL_K=4、可复现、关路不报错。"""

from app.retrieval import _apply_route_replacement, _rank_candidates, _rrf_scores
from tests.retrieval_helpers import add_memory, build_derived, make_service


def test_route_dedupe_keeps_best_rank_and_one_vote_per_id():
    ids, ranks, _hits = _apply_route_replacement(
        ["x", "y", "x"], set(), {}, {"x": 3, "y": 2}, 50
    )
    assert ids == ["x", "y"]
    assert ranks == [1, 2]  # x 只保留最佳名次 1


def test_multiple_live_heads_share_the_same_inherited_rank():
    ids, ranks, _hits = _apply_route_replacement(
        ["a", "b"],
        {"a"},
        {"a": ["h1", "h2"]},
        {"h1": 1, "h2": 2, "b": 3},
        50,
    )
    # h2 的 gseq 更新 → 排前，但两者**并列**继承 a 的名次 1；b 仍是名次 2
    assert ids == ["h2", "h1", "b"]
    assert ranks == [1, 1, 2]


def test_rrf_score_formula():
    scores, ranks = _rrf_scores(
        [("bigram", ["a", "b"]), ("trigram", ["b", "c"])], k=60
    )
    assert scores["a"] == 1 / 61
    assert scores["b"] == 1 / 62 + 1 / 61
    assert scores["c"] == 1 / 62
    assert ranks["b"] == {"bigram": 2, "trigram": 1}


def test_rrf_k_is_configurable():
    scores_small, _ = _rrf_scores([("bigram", ["a", "b"])], k=10)
    scores_default, _ = _rrf_scores([("bigram", ["a", "b"])], k=60)
    assert scores_small["a"] == 1 / 11
    assert scores_default["a"] == 1 / 61
    assert scores_small["b"] == 1 / 12


def test_tie_break_same_score_higher_gseq_wins():
    details = {
        "a": {"gseq": 1},
        "b": {"gseq": 2},
        "c": {"gseq": 3},
    }
    scores = {"a": 0.5, "b": 0.5, "c": 0.5}
    # 分相同 → gseq 大者优先（新者优先），与 recorded_at 无关
    assert _rank_candidates(scores, {"a": 1, "b": 2, "c": 3}, details) == [
        "c",
        "b",
        "a",
    ]


def test_final_k_is_4(db):
    for i in range(10):
        add_memory(db, f"北京出差记录 {i}", session_id=f"s{i}")
    build_derived(db)
    service = make_service(db, routes=("bigram",))
    result = service.search("北京出差")
    assert service.cfg.final_k == 4
    assert len(result["items"]) == 4
    assert result["limit"] == 4


def test_fusion_reproducible_same_input_two_runs(db):
    for i in range(5):
        add_memory(db, f"北京出差记录 {i}", session_id=f"s{i}")
    build_derived(db)
    service = make_service(db, routes=("bigram",))
    first = service.search("北京出差")
    second = service.search("北京出差")
    assert [(i["id"], i["score"], i["tags"]) for i in first["items"]] == [
        (i["id"], i["score"], i["tags"]) for i in second["items"]
    ]
    other = make_service(db, routes=("bigram",))
    third = other.search("北京出差")
    assert [i["id"] for i in first["items"]] == [i["id"] for i in third["items"]]


def test_two_route_fusion_scores_and_ordering(db):
    both = add_memory(db, "北京出差计划", session_id="s1")
    one = add_memory(db, "北京北京其他记录", session_id="s2")
    for i in range(5):
        add_memory(db, f"填充 {i}", session_id=f"fill-{i}")
    build_derived(db)
    service = make_service(db, routes=("bigram", "trigram"))
    result = service.search("北京出差")
    items = {item["id"]: item for item in result["items"]}
    assert both in items and one in items
    assert items[both]["score"] > items[one]["score"]
    assert set(items[both]["routes"]) == {"bigram", "trigram"}
    assert items[one]["routes"] == ["bigram"]
    # both 在两路都第 1 → 2×(1/61)；one 在 bigram 第 2 → 1/62
    assert items[both]["score"] == round(1 / 61 + 1 / 61, 12)
    assert items[one]["score"] == round(1 / 62, 12)
    assert [item["id"] for item in result["items"]][0] == both


def test_mmr_is_not_implemented_in_v8(db, monkeypatch):
    """旧 MMR 测试改写：v8 §12 明确不做 MMR，配置面与输出面都不再有它。"""
    from app.config import load_config

    monkeypatch.delenv("MMR_ENABLED", raising=False)
    cfg = load_config()
    assert not hasattr(cfg, "mmr_enabled")
    assert not hasattr(cfg, "mmr_lambda")
    add_memory(db, "北京出差记录", session_id="s1")
    build_derived(db)
    result = make_service(db, routes=("bigram",)).search("北京出差")
    assert "mmr" not in result


def test_all_routes_disabled_is_no_match_without_recent_fallback(db):
    add_memory(db, "北京出差记录", session_id="s1")
    build_derived(db)
    service = make_service(db, routes=())
    result = service.search("北京出差")
    assert result["items"] == []
    assert result["hint"].startswith("NO_MATCH")
    assert result["routes"] == {}


def test_disabled_vector_route_does_not_touch_embedding_config(db):
    add_memory(db, "北京出差记录", session_id="s1")
    build_derived(db)
    service = make_service(db, routes=("bigram",), provider=None)
    assert "vector" not in service.routes
    assert service.search("北京出差")["items"]
