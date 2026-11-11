"""验收 §9.1：基线精确再现（v8 §3.2 / D）。

产品测试只读 ``tests/fixtures/recall_baseline.json``（不 import research 代码）：
- 用产品查询构造器重算 40 题表达式，断言与 fixture **逐字节相等**；
- 用产品索引 + bigram 路重算 top-6，断言与基线 top-6 完全一致；
- R@6 = 0.4875（容差 ≤0.01，即一条不能翻）；
- fixture 必须列出 2/40 空结果题的 id 与查询串。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app import textnorm
from tests.retrieval_helpers import build_derived, make_service

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "recall_baseline.json"


def _load_fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _sha256(payload: dict) -> str:
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def test_fixture_integrity_and_metadata():
    data = _load_fixture()
    payload = {"corpus": data["corpus"], "queries": data["queries"]}
    assert _sha256(payload) == data["sha256"]
    assert data["baseline"]["source"].endswith("recall_eval.py::bigrams_of")
    assert data["baseline"]["commit"] and data["baseline"]["commit"] != "unknown"
    assert data["baseline"]["n_docs"] == len(data["corpus"]) == 119
    assert data["baseline"]["n_queries"] == len(data["queries"]) == 40
    assert data["baseline"]["expected_r6"] == pytest.approx(0.4875, abs=1e-6)


def test_fixture_lists_two_empty_queries():
    data = _load_fixture()
    empty = data["empty_queries"]
    assert [item["sid"] for item in empty] == [23, 38]
    assert data["baseline"]["empty_count"] == 2
    for item in empty:
        assert item["query"]
        assert item["expr"]
        # 该题在全量记录里 top-6 必须为空
        record = next(q for q in data["queries"] if q["sid"] == item["sid"])
        assert record["top6"] == []
        assert record["top50"] == []


def test_query_builder_is_byte_identical_to_baseline():
    data = _load_fixture()
    for record in data["queries"]:
        tokens, _truncated = textnorm.query_tokens(record["query"])
        assert textnorm.fts_match_expr(tokens) == record["expr"], record["sid"]


def test_index_and_r6_reproduce_baseline_exactly(db):
    data = _load_fixture()
    from app.write_path import create_memory

    fixture_index: dict[str, int] = {}
    for index, text in enumerate(data["corpus"]):
        receipt = create_memory(
            db,
            client_write_id=f"baseline-{index}",
            content=text,
            session_id=f"baseline-{index}",
            source_client="baseline",
        )
        fixture_index[receipt["id"]] = index
    build_derived(db)
    service = make_service(db, routes=("bigram",), route_top_k=50)

    hits = []
    exact = 0
    for record in data["queries"]:
        outcome = service.bigram_route(record["query"], 50)
        ranking = [fixture_index[mid] for mid in outcome.raw_ids]
        # bigram 路的原始名次是 bm25 ASC, gseq ASC（= 基线 rowid ASC，见
        # docs/02 §6 实测注记），因此与 fixture 必须**逐位一致**，不只是集合相等。
        # 全 40 题 top-6 顺序一致；top-50 也逐位一致（实测），故在这里直接断言。
        assert ranking[:6] == record["top6"], record["sid"]
        assert ranking == record["top50"], record["sid"]
        exact += ranking[:6] == record["top6"]
        gold = set(record["gold"])
        hits.append(len(set(ranking[:6]) & gold) / max(1, len(gold)))
    r6 = sum(hits) / len(hits)
    assert r6 == pytest.approx(0.4875, abs=0.01)
    # 收紧到 40/40：原 `exact >= 39` 的 1 题容差是 v8 实现初稿遗留的猜测
    # （当时注释误以为 bigram 原始名次用 gseq DESC）。逐题实测**没有**不一致的题：
    # 产品 tie-break 是 gseq ASC，与基线 rowid ASC 完全同序，故不存在需要写进
    # fixture 的偏差。
    assert exact == len(data["queries"]), (
        f"top-6 顺序不一致的题：{len(data['queries']) - exact}"
    )
    # 2/40 空结果题在索引侧也必须为空（不是查询构造差异）
    empty_sids = {item["sid"] for item in data["empty_queries"]}
    for record in data["queries"]:
        if record["sid"] in empty_sids:
            assert service.bigram_route(record["query"], 50).raw_ids == []
