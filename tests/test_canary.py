"""v8 §9.12 金丝雀：单 CJK 段 ≥6 字子串、只对 bigram 严格、不计统计、连续两次告警。"""

from __future__ import annotations

from app.retrieval import RouteOutcome
from tests.retrieval_helpers import add_memory, build_derived, make_service

# 每条的 6 字子串都不同，保证 canary 查询能唯一命中被抽中的记录
CORPUS = [
    "甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉",
    "春夏秋冬风花雪月山川湖海江河草木虫鱼鸟兽",
    "金木水火土东南西北中上下左右前后内外大小",
    "红橙黄绿青蓝紫黑白灰粉棕银铜铁锡铅汞钨钼",
]


def _seed(db, count: int = 20):
    for i in range(count):
        base = CORPUS[i % len(CORPUS)]
        add_memory(db, base + f"第{i:03d}条记录", session_id=f"s{i}")
    build_derived(db)


def test_canary_passes_and_does_not_count_retrieval_stats(db):
    _seed(db)
    service = make_service(db, routes=("bigram",))
    before = service.stats.snapshot()["queries"]
    report = service.run_canary()
    assert report["status"] == "ok"
    assert report["sampled"] == 20
    assert report["failure_rate"] == 0.0
    assert service.stats.snapshot()["queries"] == before  # canary 不计统计


def test_canary_insufficient_samples(db):
    add_memory(db, "短", session_id="s1")
    build_derived(db)
    service = make_service(db, routes=("bigram",))
    report = service.run_canary()
    assert report["status"] == "insufficient_samples"
    assert report["failure_rate"] is None


def test_canary_alerts_after_two_consecutive_high_failure_rates(db):
    _seed(db)
    service = make_service(db, routes=("bigram",))
    from app.retrieval import RouteOutcome

    def always_miss(self, query, limit):
        return RouteOutcome(name="bigram", raw_ids=[], token_count=1)

    service.bigram_route = always_miss.__get__(service, type(service))
    first = service.run_canary()
    assert first["failure_rate"] == 1.0
    assert first["alert"] is False  # 单次不告警
    second = service.run_canary()
    assert second["failure_rate"] == 1.0
    assert second["alert"] is True
    assert len(second["failed_ids"]) == 20
    # 告警只标记，不丢弃失败 id
    assert service.canary_state()["failed_ids"] == second["failed_ids"]


def test_canary_only_asserts_bigram_route(db):
    _seed(db)
    service = make_service(db, routes=("bigram",))
    calls = []
    original = service.bigram_route

    def spy(query, limit):
        calls.append(query)
        return original(query, limit)

    service.bigram_route = spy
    service.run_canary()
    assert calls  # 只走 bigram 路
