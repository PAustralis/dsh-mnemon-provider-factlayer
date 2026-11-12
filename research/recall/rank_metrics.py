# -*- coding: utf-8 -*-
"""生产指标与失败归因：向量单路 R@1 / R@4 / R@6 / R@20 / MRR + 逐题 gold 排名。

为什么单独一个脚本
------------------
``real_embedding_results.json`` 只聚合了 R@6 / R@10 / MRR，没有逐题排名。而
provider 契约的 ``FINAL_K`` 实际是 **4**（``docs/05`` 的返回条数），所以线上指标
是 **R@4**；R@6 只是评测期的历史口径。另需 R@20 来度量「候选池里有、但排不进前
4」的缺口（R@20 − R@4）。

口径（与 ``run_real_embedding.py`` 逐字一致，直接 import 复用）
----------------------------------------------------------------
- 语料 119 条 / 题库 40 题；query = 题库【用户问】原文，只做 NFKC；
- golden = 题库记忆条目按 NFKC 精确映射回语料的记录（优先同 scenario）；
- 向量 = ``.embedding_cache.json`` 里的真实 embedding（qwen3.7-text-embedding，
  dim=1024），键 = ``sha256(model + "\\x00" + text)``，与写缓存时同一算法；
- 余弦排序（向量已 L2 归一化，点积即余弦），stable argsort 与产品同序；
- ``--route bigram`` 走产品 ``app.textnorm`` 构造的单混合列 FTS5(unicode61)
  + ``bm25 ASC, rowid ASC``，与 ``run_real_embedding.build_bigram_db`` 同构；
  构造器一致性做逐 token 冻结校验（不一致直接退出）。

**钱**：本脚本不构造 provider、不发任何网络请求、不打开 socket，向量只从缓存
读；缓存缺一条就整体退出并报告缺哪条，绝不回落到联网补算。所以每次运行都是
0 次 API 调用、0 token。命中率不是 100% 视为缓存损坏/模型变更，同样退出。
（``httpx`` 会被 ``run_real_embedding`` 在 import 期一并载入，但这不等于联网：
本脚本从不实例化 client，也就没有请求。）

指标
----
``R@k = |top-k ∩ gold| / |gold|``，40 题**宏平均**（与产品口径一致，gold 大的题
权重被摊薄）；``MRR`` = 第一个 gold 的倒数排名，取不到记 0；``R@20 − R@4`` 是
「池里有但排不进」的比例。oracle 上界按 ``mean(min(k,|gold|)/|gold|)`` 算：|gold|=6
的题在 R@4 上最多只能拿 4/6，这是口径自带的天花板，不是检索能力。

失败归因
--------
对每个「gold 不在 top-4」的 (query, gold) 对算四个机械特征：向量名次、余弦、
与 query 的二元组词面交集、bigram 路名次。**两种 route 都建 bigram 索引**：
vector 路由下它是独立的交叉判据（否则 `bigram_rank` 恒为 None，① 的「bigram
也搜不到」就只是没算过的假设）；bigram 路由下它就是主排序本身。互斥分类：

- ①（无法召回）名次 > 20 且 词面交集 = 0 且 bigram 前 50 也搜不到；
- ②（排序问题）名次在 5–20；
- ③（被挤掉）名次 > 20 但词面有交集或 bigram 能搜到（候选可达却排不进来）；
- ④（映射可疑）独立交叉标记：该记录按题库原文并非回答本题所需证据。

④ 是**人工标注**（``EVIDENCE``）：只回答「这条记录是回答本题要用的证据吗」，
不改 gold 集合、不改指标定义，仅用于把「检索错」与「标注口径错」分开。标注依据
写进 ``--json`` 的 ``curation`` 字段，报告里逐条列。

用法::

    python research/recall/rank_metrics.py                    # 向量单路
    python research/recall/rank_metrics.py --route bigram     # 二元组单路（对照）
    python research/recall/rank_metrics.py --json out.json    # 指定 JSON 落盘路径
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent
REPO_ROOT = BASE.parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(BASE))

from recall_eval import bigrams_of, norm, parse_qbank  # noqa: E402

from app import textnorm  # noqa: E402

# 复用既有口径：与 run_real_embedding.py 完全同一份语料/题库路径、同一映射、
# 同一缓存键。该模块顶层不联网（不调用 provider，只在 main() 里才发请求）。
from run_real_embedding import (  # noqa: E402
    CACHE_PATH,
    EVAL_FINAL_K,
    ROUTE_TOP_K,
    _cache_key,
    load_inputs,
)

OUT_PATH = BASE / "rank_metrics.json"
KS = (1, 3, 4, 6, 20)
FINAL_K_PROD = 4          # docs/05 provider 契约：一次只回 4 条
BOOTSTRAP_N = 2000
SEED = 20261111
BASELINE_R4 = 0.8125      # 见 docs/08；不自证，只做回归哨兵
BASELINE_R6 = 0.8729      # 与 real_embedding_results.json 的 R1 单路对齐

# 人工标注：每一题**回答本题所需**的语料记录（gold 的子集）。
# 标注方法：逐题读题库原文 + 该场景全部记录，只回答「要回答这个问题，必须看到
# 哪几条」；同场景里对答题无贡献的记录（冰箱结霜、鸡蛋涨价之类）不计入。
# 不改 gold，不参与主指标，只用于归因与 secondary 指标。
EVIDENCE: dict[int, tuple[int, ...]] = {
    1: (0, 1),
    2: (2, 3),
    3: (5, 6, 7),
    4: (8, 9),
    5: (10,),
    6: (16, 17, 18),
    7: (20, 21),
    8: (23, 24, 25, 26),
    9: (27, 28),
    10: (29, 30),
    11: (32,),
    12: (34, 35, 36),
    13: (37, 38),
    14: (40, 41),
    15: (42,),
    16: (48, 49),
    17: (51, 52),
    18: (55, 56, 57),
    19: (58, 59),
    20: (60, 61, 62),
    21: (63, 64),
    22: (65, 66),
    23: (67, 68),
    24: (70, 71),
    25: (72,),
    26: (78, 79),
    27: (80, 81, 82),
    28: (83, 84, 85),
    29: (86, 87),
    30: (88, 89, 90),
    31: (91, 92),
    32: (93, 94),
    33: (95, 96, 97),
    34: (98, 99),
    35: (100,),
    36: (106, 107),
    37: (108, 109),
    38: (111, 112, 113),
    39: (114, 115),
    40: (116, 117, 118),
}

# 题面缺陷：问题问的实体在**全部 119 条语料**里都不存在（用关键词全文扫描确认），
# 因此该题的 gold 里不可能有正确答案，R@k 对它是「无解」而非「检索失败」。
QUERY_DEFECTS: dict[int, str] = {
    7: "问「尿酸值是多少」，全语料只有 d=97（scenario 33）提到尿酸且没给数值；"
       "本题 gold（19/20/21/22）里没有任何一条含尿酸值 → 无解",
    17: "问「酒店叫什么名字」，全语料 0 条含“酒店”；gold（51–54）只有机票/行程/购物 "
        "→ 无解",
    27: "问「车险哪天到期」，全语料 0 条含“到期/续保日期”；gold（80–82）只有续保、"
        "出险、推销电话 → 无解",
    37: "问「宠物医院叫什么名字」，全语料 0 条含“医院”；gold（108–110）只有疫苗与体重 "
        "→ 无解",
}


# ------------------------------------------------------------------ 缓存 / 数据
def load_cache() -> tuple[str, dict[str, list[float]]]:
    if not CACHE_PATH.exists():
        raise SystemExit(
            f"缓存不存在：{CACHE_PATH}\n"
            "本脚本不联网，缺缓存直接停（绝不回落到付费补算）。"
        )
    payload = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    model = payload.get("model")
    vectors = payload.get("vectors") or {}
    if not model or not vectors:
        raise SystemExit(f"缓存文件内容不完整：{CACHE_PATH}")
    return model, vectors


def build_matrix(
    model: str, vectors: dict[str, list[float]], texts: list[str]
) -> tuple[np.ndarray, dict]:
    """按缓存键取全部向量并 L2 归一化；缺一条就退出（不补算）。"""
    keys = [_cache_key(model, text) for text in texts]
    missing = [text for key, text in zip(keys, texts) if key not in vectors]
    if missing:
        raise SystemExit(
            f"缓存命中率不是 100%：缺 {len(missing)}/{len(texts)} 条，"
            f"例如 {missing[:2]!r}\n按约定停下来，不联网、不花钱。"
        )
    matrix = np.asarray([vectors[key] for key in keys], dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    matrix = matrix / norms
    return matrix, {
        "cache_path": str(CACHE_PATH),
        "model": model,
        "dim": int(matrix.shape[1]),
        "texts": len(texts),
        "cache_hits": len(texts),
        "cache_misses": 0,
        "api_calls": 0,
        "note": "只读 .embedding_cache.json；不构造 provider、不发请求",
    }


def check_constructors(queries: list[dict]) -> None:
    """产品 bigram 构造器 vs 原型构造器逐 token 相同（否则 bigram 路不可比）。"""
    from recall_eval import fts_match_expr as proto_fts_match_expr

    for item in queries:
        tokens, _truncated = textnorm.query_tokens(item["q"])
        if textnorm.fts_match_expr(tokens) != proto_fts_match_expr(bigrams_of(item["q"])):
            raise SystemExit(f"查询构造器不一致 sid={item['sid']}")
    for row in CORPUS:
        if textnorm.ngram_tokens(row["text"]) != bigrams_of(norm(row["text"])):
            raise SystemExit(f"索引构造器不一致：{row['text'][:30]!r}")


def build_bigram_index() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.execute("PRAGMA journal_mode=OFF")
    con.execute("CREATE VIRTUAL TABLE fts_bi USING fts5(bg, tokenize='unicode61')")
    for index, row in enumerate(CORPUS):
        con.execute(
            "INSERT INTO fts_bi(rowid, bg) VALUES (?,?)",
            (index, textnorm.ngram_text(row["text"])),
        )
    con.commit()
    return con


def rank_bigram(con: sqlite3.Connection, query: str, limit: int = ROUTE_TOP_K) -> list[int]:
    tokens, _truncated = textnorm.query_tokens(query)
    expr = textnorm.fts_match_expr(tokens)
    if not expr:
        return []
    sql = (
        "SELECT rowid FROM fts_bi WHERE fts_bi MATCH ? "
        "ORDER BY bm25(fts_bi) ASC, rowid ASC LIMIT ?"
    )
    try:
        return [row[0] for row in con.execute(sql, (expr, limit)).fetchall()]
    except sqlite3.OperationalError:
        return []


# ------------------------------------------------------------------ 指标
def recall_at(ranking: list[int], gold: list[int], k: int) -> float:
    if not gold:
        return 0.0
    return len(set(ranking[:k]) & set(gold)) / len(gold)


def first_gold_rank(ranking: list[int], gold: list[int]) -> int | None:
    gold_set = set(gold)
    for position, doc in enumerate(ranking, 1):
        if doc in gold_set:
            return position
    return None


def oracle_recall(gold_sizes: list[int], k: int) -> float:
    """gold 集合固定为整场景时，R@k 的结构上限（不是模型能力）。"""
    return float(np.mean([min(k, size) / size for size in gold_sizes]))


def bootstrap_ci(values: np.ndarray, *, n: int = BOOTSTRAP_N, seed: int = SEED) -> dict:
    rng = np.random.default_rng(seed)
    count = len(values)
    idx = rng.integers(0, count, size=(n, count))
    means = values[idx].mean(axis=1)
    low, high = np.percentile(means, [2.5, 97.5])
    return {
        "mean": float(values.mean()),
        "ci95": [float(low), float(high)],
        "se": float(means.std(ddof=1)),
        "n_resamples": n,
        "seed": seed,
    }


def classification(
    rank: int | None, lex_overlap: int, bigram_rank: int | None, k: int = FINAL_K_PROD
) -> str:
    """互斥机械分类（只看排名与可达性，不含人工标注）。"""
    if rank is not None and k < rank <= 20:
        return "2_rank_5_20"
    if rank is not None and 1 <= rank <= k:
        return "hit"
    if lex_overlap > 0 or bigram_rank is not None:
        return "3_displaced"
    return "1_unreachable"


CATEGORY_LABEL = {
    "1_unreachable": "① 词面/语义都不重合、排名>20（无法召回）",
    "2_rank_5_20": "② 召回了但排在 5–20（排序问题）",
    "3_displaced": "③ 被其他记录挤掉（>20 但候选可达）",
}


# ------------------------------------------------------------------ main
CORPUS: list[dict] = []  # load_inputs 之后填充，构造器校验要用


def main(argv: list[str] | None = None) -> int:
    global CORPUS
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--route", choices=("vector", "bigram"), default="vector")
    parser.add_argument("--json", default=str(OUT_PATH))
    parser.add_argument("--bootstrap", type=int, default=BOOTSTRAP_N)
    args = parser.parse_args(argv)

    corpus, queries, _scenarios = load_inputs()
    CORPUS = corpus
    check_constructors(queries)
    print(
        f"语料 {len(corpus)} 条 / 题库 {len(queries)} 题；"
        f"gold 合计 {sum(len(q['gold']) for q in queries)} 条"
    )

    model, vectors = load_cache()
    query_texts = [item["q"] for item in queries]
    doc_texts = [norm(row["text"]) for row in corpus]
    matrix, cache_meta = build_matrix(model, vectors, doc_texts + query_texts)
    doc_matrix = matrix[: len(corpus)]
    query_matrix = matrix[len(corpus) :]
    print(
        f"缓存命中 {cache_meta['cache_hits']}/{cache_meta['texts']} 条"
        f"（model={model}, dim={cache_meta['dim']}），本次 API 调用 0 次"
    )

    # 两种 route 都建：vector 路由下 bigram 名次是失败归因的独立交叉判据，
    # 不能因为「主排序不是 bigram」就省掉（那会把「没算过」当成「搜不到」）。
    con = build_bigram_index()
    bigram_cache: dict[str, list[int]] = {}
    for item in queries:
        bigram_cache[item["q"]] = rank_bigram(con, item["q"])

    def _bigram_rank_of(query: str, doc: int) -> int | None:
        ranking = bigram_cache.get(query)
        if ranking is None:
            ranking = rank_bigram(con, query)
            bigram_cache[query] = ranking
        return ranking.index(doc) + 1 if doc in ranking else None

    per_query: list[dict] = []
    for index, item in enumerate(queries):
        gold = item["gold"]
        if args.route == "vector":
            sims = doc_matrix @ query_matrix[index]
            ranking = [int(i) for i in np.argsort(-sims, kind="stable")]
            sim_by_doc = {doc: float(sims[doc]) for doc in range(len(corpus))}
        else:
            ranking = bigram_cache[item["q"]]
            sim_by_doc = {}
        position = {doc: pos for pos, doc in enumerate(ranking, 1)}
        gold_ranks = [position.get(doc) for doc in gold]
        top4 = ranking[:FINAL_K_PROD]
        evidence = set(EVIDENCE.get(item["sid"], ()))
        entries = []
        for doc in gold:
            rank = position.get(doc)
            lex = len(set(bigrams_of(item["q"])) & set(bigrams_of(doc_texts[doc])))
            bigram_rank = _bigram_rank_of(item["q"], doc)
            entries.append(
                {
                    "doc": doc,
                    "rank": rank,
                    "sim": round(sim_by_doc[doc], 6) if sim_by_doc else None,
                    "scenario": corpus[doc].get("scenario"),
                    "raw_date": corpus[doc].get("raw_date"),
                    "text": corpus[doc]["text"],
                    "lex_overlap": lex,
                    "bigram_rank": bigram_rank,
                    "is_evidence": doc in evidence,
                    "category": classification(rank, lex, bigram_rank),
                }
            )
        per_query.append(
            {
                "sid": item["sid"],
                "q": item["q"],
                "gold": gold,
                "gold_n": len(gold),
                "gold_ranks": gold_ranks,
                "r1": recall_at(ranking, gold, 1),
                "r3": recall_at(ranking, gold, 3),
                "r4": recall_at(ranking, gold, 4),
                "r6": recall_at(ranking, gold, 6),
                "r20": recall_at(ranking, gold, 20),
                "mrr": 1.0 / first_gold_rank(ranking, gold)
                if first_gold_rank(ranking, gold)
                else 0.0,
                "top4": [
                    {
                        "doc": doc,
                        "sim": round(sim_by_doc[doc], 6) if sim_by_doc else None,
                        "scenario": corpus[doc].get("scenario"),
                        "raw_date": corpus[doc].get("raw_date"),
                        "text": corpus[doc]["text"],
                        "is_evidence": doc in evidence,
                    }
                    for doc in top4
                ],
                "gold_detail": entries,
                "query_defect": QUERY_DEFECTS.get(item["sid"]),
            }
        )

    metrics = {
        "r1": float(np.mean([row["r1"] for row in per_query])),
        "r3": float(np.mean([row["r3"] for row in per_query])),
        "r4": float(np.mean([row["r4"] for row in per_query])),
        "r6": float(np.mean([row["r6"] for row in per_query])),
        "r20": float(np.mean([row["r20"] for row in per_query])),
        "mrr": float(np.mean([row["mrr"] for row in per_query])),
    }
    metrics["r20_minus_r4"] = metrics["r20"] - metrics["r4"]
    gold_sizes = [row["gold_n"] for row in per_query]
    oracle = {"r4": oracle_recall(gold_sizes, 4), "r20": oracle_recall(gold_sizes, 20)}
    # 平均命中条数（绝对条数，避免 |gold| 大的题被 1/|gold| 摊薄）
    metrics["hits4_mean"] = float(
        np.mean(
            [
                sum(1 for entry in row["gold_detail"] if entry["rank"] and entry["rank"] <= 4)
                for row in per_query
            ]
        )
    )
    metrics["hits4_total"] = int(
        sum(
            sum(1 for entry in row["gold_detail"] if entry["rank"] and entry["rank"] <= 4)
            for row in per_query
        )
    )
    # 题级命中率：至少一条 gold 出现在第 1 / 前 4 名的题占比
    # （与 R@k 不同：不除以 |gold|，衡量「这题有没有答对东西」而不是「证据全不全」）
    metrics["any_hit1"] = float(
        np.mean([1.0 if any(e["rank"] == 1 for e in row["gold_detail"]) else 0.0 for row in per_query])
    )
    metrics["any_hit4"] = float(
        np.mean(
            [
                1.0
                if any(e["rank"] and e["rank"] <= 4 for e in row["gold_detail"])
                else 0.0
                for row in per_query
            ]
        )
    )

    ci = {
        name: bootstrap_ci(
            np.asarray([row[name] for row in per_query], dtype=float),
            n=args.bootstrap,
        )
        for name in ("r1", "r3", "r4", "r6", "r20", "mrr")
    }
    ci["r20_minus_r4"] = bootstrap_ci(
        np.asarray([row["r20"] - row["r4"] for row in per_query], dtype=float),
        n=args.bootstrap,
    )

    # ---- 失败归因 ---------------------------------------------------
    misses = [
        (row, entry) for row in per_query for entry in row["gold_detail"]
        if entry["category"] != "hit"
    ]
    total_pairs = sum(row["gold_n"] for row in per_query)
    by_category: dict[str, list] = defaultdict(list)
    for row, entry in misses:
        by_category[entry["category"]].append(
            {
                "sid": row["sid"],
                "q": row["q"],
                "doc": entry["doc"],
                "rank": entry["rank"],
                "sim": entry["sim"],
                "lex_overlap": entry["lex_overlap"],
                "bigram_rank": entry["bigram_rank"],
                "is_evidence": entry["is_evidence"],
                "scenario": entry["scenario"],
                "raw_date": entry["raw_date"],
                "text": entry["text"],
                "top4": [
                    {
                        "doc": item["doc"],
                        "scenario": item["scenario"],
                        "sim": item["sim"],
                        "is_evidence": item["is_evidence"],
                        "text": item["text"],
                    }
                    for item in row["top4"]
                ],
            }
        )
    miss_total = len(misses)
    labels = {key: CATEGORY_LABEL[key] for key in CATEGORY_LABEL}
    category_table = {
        key: {
            "label": labels.get(key, key),
            "count": len(by_category[key]),
            "share": len(by_category[key]) / miss_total if miss_total else 0.0,
            "evidence_count": sum(1 for x in by_category[key] if x["is_evidence"]),
        }
        for key in ("1_unreachable", "2_rank_5_20", "3_displaced")
    }
    evidence_misses = [x for group in by_category.values() for x in group if x["is_evidence"]]
    noise_misses = [x for group in by_category.values() for x in group if not x["is_evidence"]]

    # R@20 − R@4 的精确分解：只有「5 ≤ 名次 ≤ 20」的对才同时被 R@4 漏掉、被 R@20 捞到，
    # 它们才是差值的来源。按 1/|gold| 加权（与宏平均口径一致）后分「证据 / 噪声」。
    gap_parts = {"evidence": 0.0, "not_evidence": 0.0}
    for row, entry in misses:
        rank = entry["rank"]
        if rank is not None and rank <= 20:
            share = 1.0 / row["gold_n"] / len(per_query)
            gap_parts["evidence" if entry["is_evidence"] else "not_evidence"] += share
    gap_decomposition = {
        "total": gap_parts["evidence"] + gap_parts["not_evidence"],
        "evidence": gap_parts["evidence"],
        "not_evidence": gap_parts["not_evidence"],
        "pairs_5_20": sum(
            1 for _row, entry in misses if entry["rank"] is not None and entry["rank"] <= 20
        ),
        "pairs_gt_20": sum(
            1 for _row, entry in misses if entry["rank"] is None or entry["rank"] > 20
        ),
        "note": "R@20−R@4 只由 5–20 名的漏项贡献；>20 的对两个指标都漏，只拉低两者",
    }

    # 2×2 交叉表：证据相关 × 名次桶（用于说明「④ 与 ① 重叠」）
    evidence_5_20 = sum(
        1 for x in evidence_misses if x["rank"] is not None and 5 <= x["rank"] <= 20
    )
    cross = {
        "evidence": {
            "rank_5_20": evidence_5_20,
            "rank_gt_20": len(evidence_misses) - evidence_5_20,
        },
        "not_evidence": {
            "rank_5_20": sum(
                1 for x in noise_misses if x["rank"] is not None and 5 <= x["rank"] <= 20
            ),
            "rank_gt_20": sum(
                1
                for x in noise_misses
                if x["rank"] is None or x["rank"] > 20
            ),
        },
    }

    # secondary：只按「证据对」算的召回（口径修正后的下界视角）
    evidence_pairs = [
        (row, entry) for row in per_query for entry in row["gold_detail"] if entry["is_evidence"]
    ]
    evidence_hits4 = sum(
        1 for _row, entry in evidence_pairs if entry["rank"] is not None and entry["rank"] <= 4
    )
    evidence_hits20 = sum(
        1 for _row, entry in evidence_pairs if entry["rank"] is not None and entry["rank"] <= 20
    )
    evidence_macro = [
        row
        for row in per_query
        if EVIDENCE.get(row["sid"])
    ]
    evidence_metric = {
        "pairs": len(evidence_pairs),
        "hits4": evidence_hits4,
        "hits20": evidence_hits20,
        "r4_pair": evidence_hits4 / len(evidence_pairs) if evidence_pairs else None,
        "r20_pair": evidence_hits20 / len(evidence_pairs) if evidence_pairs else None,
        "r4_macro": float(
            np.mean(
                [
                    len({e["doc"] for e in row["gold_detail"] if e["is_evidence"] and e["rank"] and e["rank"] <= 4})
                    / len(EVIDENCE[row["sid"]])
                    for row in evidence_macro
                ]
            )
        )
        if evidence_macro
        else None,
        "note": "只把 EVIDENCE 标注的记录当 gold 的次要口径；不替代主口径",
    }
    # 噪声占位：top-4 里坐了多少条非证据记录（对每题的 evidence 集合算）
    noise_slots = sum(
        1
        for row in per_query
        if EVIDENCE.get(row["sid"])
        for item in row["top4"]
        if item["doc"] not in set(EVIDENCE[row["sid"]])
    )

    # 重排上界：把 evidence-miss 全部修好能涨多少（宏平均口径，与主指标同权）
    delta_if_fixed = 0.0
    for row in per_query:
        wanted = set(EVIDENCE.get(row["sid"], ()))
        if not wanted:
            continue
        current = sum(
            1
            for entry in row["gold_detail"]
            if entry["doc"] in wanted and entry["rank"] and entry["rank"] <= 4
        )
        delta_if_fixed += (len(wanted) - current) / len(wanted)
    delta_if_fixed /= len(per_query)

    # 重排上界能不能被测出来：假设 4 个漏项全被修好，做配对 bootstrap。
    # 只有 2 题会变，所以 Δ 的分布必然在 0 上堆一大块——这正是「测不出」的量化形式。
    fixed_r4 = np.asarray(
        [
            row["r4"]
            + (
                len(EVIDENCE.get(row["sid"], ()))
                - sum(
                    1
                    for entry in row["gold_detail"]
                    if entry["doc"] in set(EVIDENCE.get(row["sid"], ()))
                    and entry["rank"]
                    and entry["rank"] <= 4
                )
            )
            / row["gold_n"]
            for row in per_query
        ],
        dtype=float,
    )
    actual_r4 = np.asarray([row["r4"] for row in per_query], dtype=float)
    rng = np.random.default_rng(SEED)
    idx = rng.integers(0, len(per_query), size=(args.bootstrap, len(per_query)))
    deltas = (fixed_r4 - actual_r4)[idx].mean(axis=1)
    delta_ci = [float(np.percentile(deltas, 2.5)), float(np.percentile(deltas, 97.5))]
    delta_ci90 = [float(np.percentile(deltas, 5)), float(np.percentile(deltas, 95))]

    # 精确率：返回 k 条时有多少槽位是「答题真要用的」（用 rank <= k 判定）
    def _evidence_slots(k: int) -> int:
        return sum(
            1
            for row in per_query
            if EVIDENCE.get(row["sid"])
            for entry in row["gold_detail"]
            if entry["doc"] in set(EVIDENCE[row["sid"]]) and entry["rank"] and entry["rank"] <= k
        )

    slots = FINAL_K_PROD * len(per_query)
    evidence_slots = _evidence_slots(FINAL_K_PROD)
    precision = {
        "p4_evidence": evidence_slots / slots if slots else None,
        "p4_ceiling": len(evidence_pairs) / slots if slots else None,
        "p3_evidence": _evidence_slots(3) / (3 * len(per_query)),
        "evidence_slots": evidence_slots,
        "slots": slots,
        "evidence_per_query": len(evidence_pairs) / len(per_query),
        "note": "FINAL_K=4 固定返回 4 条；每题平均只有 2.2 条证据记录，"
                "所以一半槽位天然是无关记录，提纯只能靠减 k / 弃权",
    }

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "route": args.route,
        "final_k_prod": FINAL_K_PROD,
        "eval_final_k": EVAL_FINAL_K,
        "route_top_k": ROUTE_TOP_K,
        "corpus": {"docs": len(corpus), "queries": len(queries), "gold_pairs": total_pairs},
        "cache": cache_meta,
        "metrics": metrics,
        "metrics_ci95": ci,
        "oracle": oracle,
        "failure": {
            "miss_pairs": miss_total,
            "pair_total": total_pairs,
            "miss_rate_pairs": miss_total / total_pairs if total_pairs else None,
            "categories": category_table,
            "cross_tab": cross,
            "gap_decomposition": gap_decomposition,
            "evidence_misses": evidence_misses,
            "noise_misses": noise_misses,
            "noise_evidence_pairs": total_pairs - len(evidence_pairs),
            "noise_slots_in_top4": noise_slots,
        },
        "evidence_metric": evidence_metric,
        "precision": precision,
        "rerank_headroom": {
            "delta_r4_macro_if_all_evidence_misses_fixed": delta_if_fixed,
            "delta_r4_paired_ci95": delta_ci,
            "delta_r4_paired_ci90": delta_ci90,
            "p_delta_gt_zero": float((deltas > 0).mean()),
            "note": "只修 evidence 标注的漏召回（不含噪声对）能拿到的宏平均上界；"
                    "CI 下界为 0 表示 n=40 无法把它与「没变」区分开",
        },
        "curation": {
            "author": "本报告（逐题对照题库原文人工标注）",
            "rule": "EVIDENCE[sid] = 回答该题所需/直接支撑的语料记录；不改 gold、不改主指标",
            "query_defects": QUERY_DEFECTS,
        },
        "per_query": per_query,
    }

    Path(args.json).write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    # ---- stdout -----------------------------------------------------
    print(f"\n== 指标（{len(queries)} 题宏平均，route={args.route}） ==")
    print(f"{'':<14}{'R@1':>8}{'R@3':>8}{'R@4':>8}{'R@6':>8}{'R@20':>8}{'MRR':>8}")
    print(
        f"{'实测':<14}{metrics['r1']:>8.4f}{metrics['r3']:>8.4f}{metrics['r4']:>8.4f}"
        f"{metrics['r6']:>8.4f}{metrics['r20']:>8.4f}{metrics['mrr']:>8.4f}"
    )
    print(
        f"{'95% CI R@4':<14}{'':>8}({ci['r4']['ci95'][0]:.4f}, {ci['r4']['ci95'][1]:.4f})"
        f"  SE={ci['r4']['se']:.4f}"
    )
    print(f"  R@20 − R@4 = {metrics['r20_minus_r4']:.4f}")
    print(
        f"    其中 5–20 名漏项 {gap_decomposition['pairs_5_20']} 个贡献全部差值："
        f"证据 {gap_decomposition['evidence']:+.4f} / 噪声 "
        f"{gap_decomposition['not_evidence']:+.4f}；"
        f">20 名漏项 {gap_decomposition['pairs_gt_20']} 个对差值无贡献"
    )
    print(
        f"  题级命中：any-hit@1={metrics['any_hit1']:.4f}  any-hit@4={metrics['any_hit4']:.4f}"
        f"（不除以 |gold|）"
    )
    print(f"  口径上限（|gold| 决定的 oracle）：R@4={oracle['r4']:.4f}  R@20={oracle['r20']:.4f}")

    print(f"\n== 失败归因（{miss_total}/{total_pairs} 个 gold 对不在 top-{FINAL_K_PROD}） ==")
    for key in ("1_unreachable", "2_rank_5_20", "3_displaced"):
        row = category_table[key]
        print(
            f"  {row['label']:<40}{row['count']:>4}  {row['share']*100:>5.1f}%"
            f"   （其中实为证据：{row['evidence_count']}）"
        )
    print(
        f"  ④ 非证据记录（映射/口径可疑，横切）：{len(noise_misses)}"
        f" / {miss_total} = {len(noise_misses)/miss_total*100:.1f}%"
    )
    print(
        f"  真正该召回的漏项：{len(evidence_misses)} 个，"
        f"名次 {[x['rank'] for x in evidence_misses]}"
    )
    print(
        f"  证据口径：R@4={evidence_metric['r4_pair']:.4f} (pair) / "
        f"{evidence_metric['r4_macro']:.4f} (macro)，"
        f"R@20={evidence_metric['r20_pair']:.4f}"
    )
    print(f"  重排上界（修完全部证据漏项）：ΔR@4 = {delta_if_fixed:+.4f}，"
          f"配对 95% CI [{delta_ci[0]:+.4f}, {delta_ci[1]:+.4f}]")
    print(
        f"  精确率：P@4={precision['p4_evidence']:.4f}（上界 {precision['p4_ceiling']:.4f}，"
        f"每题平均证据 {precision['evidence_per_query']:.2f} 条 vs 固定返回 {FINAL_K_PROD} 条）"
        f"  P@3={precision['p3_evidence']:.4f}"
    )
    print(
        f"  少回一条（k=4 → k=3）：R {metrics['r3']:.4f}（−{metrics['r4'] - metrics['r3']:.4f}）"
        f" / P {precision['p3_evidence']:.4f}（{precision['p3_evidence'] - precision['p4_evidence']:+.4f}）"
    )

    if args.route == "vector":
        if abs(metrics["r6"] - BASELINE_R6) > 5e-4:
            print(f"  ! 回归哨兵：R@6={metrics['r6']:.4f} != {BASELINE_R6}（历史口径）")
        if abs(metrics["r4"] - BASELINE_R4) > 5e-4:
            print(f"  ! 回归哨兵：R@4={metrics['r4']:.4f} != {BASELINE_R4}")
    print(f"\nJSON 写入 {args.json}（0 次 API 调用、0 token）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
