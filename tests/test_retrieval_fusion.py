"""RRF 融合：分数公式、固定 tie-break、可复现、关路不报错、目标路数。"""

from app.retrieval import _rank_candidates, _rrf_scores

from tests.retrieval_helpers import add_memory, build_derived, make_service


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


def test_tie_break_same_score_newer_recorded_at_wins_then_id():
    details = {
        "b": {"recorded_at": 100},
        "a": {"recorded_at": 100},
        "c": {"recorded_at": 200},
    }
    scores = {"a": 0.5, "b": 0.5, "c": 0.5}
    # 分相同 → recorded_at 新者优先；同时间 → id 字典序
    assert _rank_candidates(scores, details) == ["c", "a", "b"]


def test_fusion_reproducible_same_input_two_runs(db):
    for i in range(5):
        add_memory(db, f"北京出差记录 {i}", session_id=f"s{i}")
    build_derived(db)
    service = make_service(db, routes=("bigram",), expand_max_items=12)
    first = service.search("北京出差")
    second = service.search("北京出差")
    assert [(i["id"], i["score"], i["routes"]) for i in first["items"]] == [
        (i["id"], i["score"], i["routes"]) for i in second["items"]
    ]
    # 另建一个服务实例（新连接/新索引），结果仍完全一致
    other = make_service(db, routes=("bigram",), expand_max_items=12)
    third = other.search("北京出差")
    assert [i["id"] for i in first["items"]] == [i["id"] for i in third["items"]]


def test_two_route_fusion_scores_and_ordering(db):
    both = add_memory(db, "北京出差计划", session_id="s1")
    one = add_memory(db, "北京北京其他记录", session_id="s2")
    # 补足候选数（≥FINAL_K）避免 R6 兜底把 recent 混进来源标注
    for i in range(5):
        add_memory(db, f"北京填充 {i}", session_id=f"fill-{i}")
    build_derived(db)
    service = make_service(db, routes=("bigram", "trigram"))
    result = service.search("北京出差")
    items = {item["id"]: item for item in result["items"]}
    assert both in items and one in items
    assert items[both]["score"] > items[one]["score"]
    assert set(items[both]["routes"]) == {"bigram", "trigram"}
    assert items[one]["routes"] == ["bigram"]
    # 精确分数：both 在两路都第 1 → 2×(1/61)；one 在 bigram 第 2 → 1/62
    assert items[both]["score"] == round(1 / 61 + 1 / 61, 9)
    assert items[one]["score"] == round(1 / 62, 9)
    assert [item["id"] for item in result["items"]][0] == both


def test_mmr_off_by_default_and_on_is_deterministic(db):
    from app.config import load_config

    assert load_config().mmr_enabled is False
    for i in range(6):
        add_memory(db, f"北京出差记录第 {i} 条", session_id=f"s{i}")
    build_derived(db)
    service = make_service(db, routes=("bigram",), mmr_enabled=True, mmr_lambda=0.7)
    first = service.search("北京出差")
    second = service.search("北京出差")
    assert [i["id"] for i in first["items"]] == [i["id"] for i in second["items"]]


def test_all_routes_disabled_falls_back_to_recent_without_error(db):
    add_memory(db, "北京出差记录", session_id="s1")
    build_derived(db)
    service = make_service(db, routes=())
    result = service.search("北京出差")
    assert result["fallback"]["recent"] is True
    assert set(result["routes"]) == {"recent"}
    assert result["items"]


def test_disabled_vector_route_does_not_touch_embedding_config(db):
    add_memory(db, "北京出差记录", session_id="s1")
    build_derived(db)
    service = make_service(db, routes=("bigram",), provider=None)
    assert "vector" not in result_routes(service)
    assert service.search("北京出差")["items"]


def result_routes(service):
    return service.routes
