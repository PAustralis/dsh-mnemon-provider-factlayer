# -*- coding: utf-8 -*-
"""从 research/recall 生成检索层 v8 的 golden fixture（一次性，可复现）。

产物：``tests/fixtures/recall_baseline.json``，产品测试**不依赖 research 代码**，
只读这个 fixture：

- 每题的基线查询串（``recall_eval.py::bigrams_of`` + ``fts_match_expr``）
- 预期 top-6（corpus 下标，按基线 fts_bi 的 bm25 排序）
- 40 题 gold（corpus 下标）
- 语料规范化文本（供产品实现建同名 FTS 索引重算并断言逐字节相等 / R@6）
- sha256（语料 + 查询记录的 canonical JSON）
- 基线 commit（生成时的仓库 HEAD）
- 2/40 空结果题的 id 与查询串

用法：
    python research/recall/make_golden_fixture.py
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent
REPO_ROOT = BASE.parent.parent
sys.path.insert(0, str(REPO_ROOT))

import baseline_r2 as proto  # noqa: E402

OUT = REPO_ROOT / "tests" / "fixtures" / "recall_baseline.json"


def baseline_rankings(corpus, queries):
    """按 recall_eval.py 的建库与检索路径重算每题 top-50。"""
    with tempfile.TemporaryDirectory() as tmp:
        con = sqlite3.connect(os.path.join(tmp, "baseline.sqlite"))
        con.execute("PRAGMA journal_mode=OFF")
        con.execute("CREATE TABLE docs(docid INTEGER PRIMARY KEY, bg TEXT)")
        con.execute("CREATE VIRTUAL TABLE fts_bi USING fts5(bg, tokenize='unicode61')")
        for index, row in enumerate(corpus):
            bg = " ".join(proto.proto_bigrams_of(proto.proto_norm(row["text"])))
            con.execute("INSERT INTO docs VALUES (?,?)", (index, bg))
            con.execute("INSERT INTO fts_bi(rowid, bg) VALUES (?,?)", (index, bg))
        con.commit()
        rankings = []
        for item in queries:
            expr = proto.proto_fts_match_expr(proto.proto_bigrams_of(item["query"]))
            rankings.append(proto.proto_rank_fts(con, "fts_bi", expr, limit=50))
        con.close()
    return rankings


def sha256_of(payload: dict) -> str:
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True
        ).strip()
    except Exception:  # pragma: no cover - 非 git 环境
        return "unknown"


def main() -> int:
    corpus, queries, unmatched = proto.load_inputs()
    assert not unmatched, f"题库记忆未全部映射回语料：{unmatched[:3]}"
    rankings = baseline_rankings(corpus, queries)

    records = []
    empty_queries = []
    hits6 = []
    for item, ranking in zip(queries, rankings):
        gold = set(item["gold"])
        top6 = [int(i) for i in ranking[:6]]
        records.append(
            {
                "sid": int(item["sid"]),
                "query": item["query"],
                "expr": proto.proto_fts_match_expr(
                    proto.proto_bigrams_of(item["query"])
                ),
                "top6": top6,
                "top50": [int(i) for i in ranking],
                "gold": sorted(int(i) for i in gold),
            }
        )
        if ranking:
            hits6.append(len(set(top6) & gold) / max(1, len(gold)))
        else:
            hits6.append(0.0)  # 空结果按 R@6=0 计入 40 题平均
            empty_queries.append(
                {
                    "sid": int(item["sid"]),
                    "query": item["query"],
                    "expr": proto.proto_fts_match_expr(
                        proto.proto_bigrams_of(item["query"])
                    ),
                    "reason": "基线 top-50 为空（查询 bigram token 在语料中无命中）",
                }
            )
    r6 = sum(hits6) / len(hits6)

    corpus_payload = [proto.proto_norm(row["text"]) for row in corpus]
    payload = {
        "corpus": corpus_payload,
        "queries": records,
    }
    fixture = {
        "schema": 1,
        "baseline": {
            "source": "research/recall/recall_eval.py::bigrams_of",
            "commit": git_commit(),
            "metric": "R@6",
            "expected_r6": round(r6, 6),
            "n_docs": len(corpus_payload),
            "n_queries": len(records),
            "empty_count": len(empty_queries),
        },
        "sha256": sha256_of(payload),
        "empty_queries": empty_queries,
        "corpus": corpus_payload,
        "queries": records,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(fixture, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    print(f"wrote {OUT}")
    print(
        f"docs={len(corpus_payload)} queries={len(records)} "
        f"empty={len(empty_queries)} R@6={r6:.4f}"
    )
    for item in empty_queries:
        print(f"  empty sid={item['sid']} query={item['query']!r} expr={item['expr']!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
