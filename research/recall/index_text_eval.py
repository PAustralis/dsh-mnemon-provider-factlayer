# -*- coding: utf-8 -*-
"""日期前缀进索引文本：**配对**评测（加前缀前 vs 后，同一批 119 条 + 40 题）。

背景（docs/08 / docs/02 v9）
---------------------------
sid 33 问「我 2025 年体检查出了什么问题?」，而「2025」只存在于 corpus 的
``raw_date`` 字段、不在正文里；纯 content 索引的向量路排不进前 4（gold 名次
9/6/5）。本评测把 ``raw_date`` 映射成事实层 ``event_time`` 后**真实入库**，比较：

- before：v1 索引文本 = content（与 ``rank_metrics.py`` 的历史口径一致）；
- after：v2 索引文本 = 日期前缀 + content（``app.index_text`` 的产品构造器）。

指标：R@1 / R@4 / R@6 / R@20、MRR、逐题 gold 名次。主口径是**向量单路**
（生产默认）；二元组单路作为第二条受影响的路单独报告（前缀同样进了 BM25 列）。

钱 / 缓存
---------
after 的 159 条文本（119 文档索引文本 + 40 查询）用 ``.embedding_cache_indexv2.json``
（新文件，已 gitignore；**不污染**旧 ``.embedding_cache.json``）。before 只读旧缓存
（0 调用）。重复运行命中新缓存 → 0 调用。价格未知，只报实测 token。

用法::

    python research/recall/index_text_eval.py
    python research/recall/index_text_eval.py --out index_text_eval.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import tempfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent
REPO_ROOT = BASE.parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(BASE))

# 与 run_real_embedding.py 相同的本机 NO_PROXY 兼容处理。
for _var in ("NO_PROXY", "no_proxy"):
    _raw = os.environ.get(_var)
    if _raw and "[::1]" in _raw:
        os.environ[_var] = _raw.replace("[::1]", "::1")

from recall_eval import norm as proto_norm  # noqa: E402
from run_real_embedding import (  # noqa: E402
    EmbeddingClient,
    _CachedEmbeddingProvider,
    _cache_key,
    _install_usage_capture,
    _latency_stats,
    load_inputs,
)

from app import textnorm  # noqa: E402
from app.config import _load_dotenv, load_config  # noqa: E402
from app.db import Database  # noqa: E402
from app.derive import DerivePipeline  # noqa: E402
from app.embeddings import build_provider, normalize_vector  # noqa: E402
from app.index_text import INDEX_TEXT_VERSION, index_text_for  # noqa: E402
from app.retrieval import RetrievalService  # noqa: E402
from app.schema import init_db  # noqa: E402
from app.write_path import create_memory  # noqa: E402
import app.write_path as write_path_module  # noqa: E402

OLD_CACHE_PATH = BASE / ".embedding_cache.json"
NEW_CACHE_PATH = BASE / ".embedding_cache_indexv2.json"
OUT_PATH = BASE / "index_text_eval.json"

# 与 run_real_embedding.load_inputs 的默认路径保持一致（那里已处理环境变量）。
# 评测时钟固定：12 条「日期不详」的记录用 recorded_at 回退，固定值让缓存可复现。
FIXED_RECORDED_AT_MS = int(
    datetime(2026, 11, 12, 0, 0, tzinfo=timezone.utc).timestamp() * 1000
)
ROUTE_TOP_K = 50
KS = (1, 4, 6, 20)
# 回归哨兵：v1 向量单路历史数字（rank_metrics.json / docs/08），容差 5e-4。
BASELINE_R4 = 0.8125
BASELINE_R6 = 0.8729

_YEAR_RE = re.compile(r"^\d{4}$")
_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ------------------------------------------------------------------ 日期映射
def event_time_from_raw_date(raw_date: str | None) -> tuple[str | None, str | None]:
    """corpus.raw_date → (event_time, event_time_precision)；「日期不详」→ (None, None)。

    只接受事实层写入路径允许的三种形状；``日期不详`` 走 recorded_at 的 UTC 回退
    （与产品一致，评测里用固定时钟）。
    """
    text = (raw_date or "").strip()
    if _YEAR_RE.match(text):
        return text, "year"
    if _MONTH_RE.match(text):
        return text, "month"
    if _DAY_RE.match(text):
        return text, "day"
    return None, None


def build_index_texts(corpus: list[dict]) -> tuple[list[str], list[tuple]]:
    """给每条语料生成 v2 索引文本（与产品 ``index_text_for`` 同一函数）。"""
    texts: list[str] = []
    dates: list[tuple] = []
    for row in corpus:
        event_time, precision = event_time_from_raw_date(row.get("raw_date"))
        dates.append((event_time, precision))
        texts.append(
            index_text_for(
                {
                    "content": row["text"],
                    "event_time": event_time,
                    "event_time_precision": precision,
                    "recorded_at": FIXED_RECORDED_AT_MS,
                }
            )
        )
    return texts, dates


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


def score_ranking(ranking: list[int], gold: list[int]) -> dict:
    rank = first_gold_rank(ranking, gold)
    return {
        **{f"r{k}": recall_at(ranking, gold, k) for k in KS},
        "mrr": (1.0 / rank) if rank else 0.0,
        "gold_ranks": [
            next((i + 1 for i, doc in enumerate(ranking) if doc == g), None) for g in gold
        ],
    }


def summarize(per_query: list[dict], key: str) -> dict:
    rows = [item[key] for item in per_query]
    out = {
        f"r{k}": float(np.mean([row[f"r{k}"] for row in rows])) for k in KS
    }
    out["mrr"] = float(np.mean([row["mrr"] for row in rows]))
    out["any_hit4"] = float(
        np.mean(
            [
                1.0
                if any(rank is not None and rank <= 4 for rank in row["gold_ranks"])
                else 0.0
                for row in rows
            ]
        )
    )
    return out


def deltas(after: dict, before: dict) -> dict:
    return {key: after[key] - before[key] for key in before}


# ------------------------------------------------------------------ 排序
def rank_matrix(doc_matrix: np.ndarray, query_matrix: np.ndarray, index: int) -> list[int]:
    sims = doc_matrix @ query_matrix[index]
    return [int(i) for i in np.argsort(-sims, kind="stable")]


def build_bigram_index(texts: list[str]) -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.execute("PRAGMA journal_mode=OFF")
    con.execute("CREATE VIRTUAL TABLE fts_bi USING fts5(bg, tokenize='unicode61')")
    for index, text in enumerate(texts):
        con.execute(
            "INSERT INTO fts_bi(rowid, bg) VALUES (?,?)",
            (index, textnorm.ngram_text(text)),
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


def paired_bootstrap(
    per_query: list[dict],
    left_key: str,
    right_key: str,
    metric: str,
    *,
    n: int = 2000,
    seed: int = 20261112,
) -> dict:
    """同题同题号配对 bootstrap：Δ = left - right（40 题重采样）。"""
    rng = np.random.default_rng(seed)
    left = np.asarray([item[left_key][metric] for item in per_query], dtype=float)
    right = np.asarray([item[right_key][metric] for item in per_query], dtype=float)
    idx = rng.integers(0, len(per_query), size=(n, len(per_query)))
    diffs = left[idx].mean(axis=1) - right[idx].mean(axis=1)
    low, high = np.percentile(diffs, [2.5, 97.5])
    return {
        "metric": metric,
        "mean": float(left.mean() - right.mean()),
        "ci95": [float(low), float(high)],
        "excludes_zero": bool(low > 0 or high < 0),
        "p_gt_zero": float((diffs > 0).mean()),
        "n_resamples": n,
        "seed": seed,
    }


def load_old_matrix(model: str, texts: list[str]) -> tuple[np.ndarray, dict]:
    """before 只读旧缓存（0 次 API 调用）；缺一条就停，不联网补算。"""
    if not OLD_CACHE_PATH.exists():
        raise SystemExit(
            f"旧缓存不存在：{OLD_CACHE_PATH}\n"
            "before 口径要求历史向量；请先跑 run_real_embedding.py 或提供缓存。"
        )
    payload = json.loads(OLD_CACHE_PATH.read_text(encoding="utf-8"))
    if payload.get("model") != model:
        raise SystemExit(
            f"旧缓存模型 {payload.get('model')!r} != 当前模型 {model!r}；拒绝混用"
        )
    vectors = payload.get("vectors") or {}
    keys = [_cache_key(model, text) for text in texts]
    missing = [text for key, text in zip(keys, texts) if key not in vectors]
    if missing:
        raise SystemExit(
            f"旧缓存缺 {len(missing)}/{len(texts)} 条（例如 {missing[:2]!r}）；"
            "按约定停止，不联网补算。"
        )
    matrix = np.asarray([vectors[key] for key in keys], dtype=np.float32)
    matrix = np.vstack([normalize_vector(row) for row in matrix])
    return matrix, {
        "cache_path": str(OLD_CACHE_PATH),
        "texts": len(texts),
        "cache_hits": len(texts),
        "api_calls": 0,
        "model": model,
        "dim": int(matrix.shape[1]),
    }


# ------------------------------------------------------------------ 产品路径复算
def run_product_path(
    corpus: list[dict],
    queries: list[dict],
    dates: list[tuple],
    index_texts: list[str],
    client: EmbeddingClient,
    cfg,
) -> dict:
    """真实入库（event_time 来自 raw_date）→ 派生重建 → 产品 RetrievalService 排序。

    与直接矩阵排序做逐位对照，确认产品路径确实使用 v2 索引文本（而不是评测脚本
    自己另算一套）。
    """
    cached = _CachedEmbeddingProvider(client, cfg.embedding_model)
    route_cfg = replace(
        cfg,
        retrieval_routes=("vector",),
        final_k=4,
        route_top_k=ROUTE_TOP_K,
        expand_max_items=4,
    )
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(os.path.join(tmp, "index-text-eval.db"))
        try:
            init_db(db)
            original_now = write_path_module.now_ms
            write_path_module.now_ms = lambda: FIXED_RECORDED_AT_MS
            receipts = []
            try:
                for i, row in enumerate(corpus):
                    event_time, precision = dates[i]
                    receipts.append(
                        create_memory(
                            db,
                            client_write_id=f"index-eval-{i}",
                            content=row["text"],
                            session_id=f"scenario-{row.get('scenario')}-{i}",
                            source_client="eval",
                            event_time=event_time,
                            event_time_precision=precision,
                        )
                    )
            finally:
                write_path_module.now_ms = original_now

            # 硬校验：库内记录按产品构造器算出的索引文本，必须与送进 embedding 的文本逐字相同。
            rows = db.read_conn.execute(
                "SELECT content, event_time, event_time_precision, recorded_at "
                "FROM memories ORDER BY gseq"
            ).fetchall()
            for i, row in enumerate(rows):
                actual = index_text_for(dict(row))
                if actual != index_texts[i]:
                    raise SystemExit(
                        f"产品入库索引文本与评测文本不一致（doc {i}）："
                        f"{actual!r} != {index_texts[i]!r}"
                    )

            pipeline = DerivePipeline(
                db,
                stub_success=True,
                embedding_provider=cached,
                embedding_dim=cfg.embedding_dim_int,
            )
            pipeline.rebuild_all()
            pending = {
                "fts": pipeline.pending_count("fts"),
                "embedding": pipeline.pending_count("embedding"),
            }
            if any(pending.values()):
                raise RuntimeError(f"派生未补齐：{pending}")

            service = RetrievalService(db, route_cfg, cached)
            id_to_index = {receipt["id"]: i for i, receipt in enumerate(receipts)}
            vector_rankings: list[list[int]] = []
            bigram_rankings: list[list[int]] = []
            for item in queries:
                vector_outcome = service.vector_route(item["q"], ROUTE_TOP_K)
                bigram_outcome = service.bigram_route(item["q"], ROUTE_TOP_K)
                vector_rankings.append(
                    [
                        id_to_index[memory_id]
                        for memory_id in vector_outcome.raw_ids
                        if memory_id in id_to_index
                    ]
                )
                bigram_rankings.append(
                    [
                        id_to_index[memory_id]
                        for memory_id in bigram_outcome.raw_ids
                        if memory_id in id_to_index
                    ]
                )
            return {
                "vector": vector_rankings,
                "bigram": bigram_rankings,
                "docs": len(corpus),
                "queries": len(queries),
                "version": INDEX_TEXT_VERSION,
            }
        finally:
            db.close()


def _compare_order(left: list[list[int]], right: list[list[int]]) -> dict:
    """逐题比较产品路径与直接排序的 top-50（不是比较 40 个榜单的长度）。"""
    mismatches = []
    for query_index, (product_ranking, direct_ranking) in enumerate(zip(left, right)):
        common = min(len(product_ranking), len(direct_ranking), ROUTE_TOP_K)
        first_diff = next(
            (
                i
                for i in range(common)
                if product_ranking[i] != direct_ranking[i]
            ),
            None,
        )
        if first_diff is not None:
            mismatches.append(
                {
                    "query_index": query_index,
                    "first_diff": first_diff,
                    "product_doc": product_ranking[first_diff],
                    "direct_doc": direct_ranking[first_diff],
                }
            )
    return {
        "identical_top50": not mismatches,
        "queries_compared": len(left),
        "mismatch_count": len(mismatches),
        "mismatches": mismatches[:5],
    }


# ------------------------------------------------------------------ main
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(OUT_PATH))
    parser.add_argument("--bootstrap", type=int, default=2000)
    args = parser.parse_args(argv)

    _load_dotenv(str(REPO_ROOT / ".env"))
    cfg = load_config()
    if not cfg.embedding_base_url or not cfg.embedding_model:
        raise SystemExit("缺少 EMBEDDING_BASE_URL / EMBEDDING_MODEL（见 .env）")

    corpus, queries, _scenarios = load_inputs()
    print(
        f"embedding: model={cfg.embedding_model} dim_cfg={cfg.embedding_dim} "
        f"docs={len(corpus)} queries={len(queries)}"
    )

    index_texts, dates = build_index_texts(corpus)
    query_texts = [item["q"] for item in queries]
    if any(textnorm.normalize(item["q"]) != item["q"] for item in queries):
        raise SystemExit("查询文本不是 textnorm.normalize 的不动点，无法与产品缓存键对齐")

    # 新缓存：159 条全部按 v2 文本重新算（旧缓存完全不碰）。
    _install_usage_capture()
    provider = build_provider(cfg)
    client = EmbeddingClient(provider, NEW_CACHE_PATH)
    started = datetime.now(timezone.utc)
    matrix_after = client.embed(index_texts + query_texts)
    after_seconds = (datetime.now(timezone.utc) - started).total_seconds()
    dim = int(matrix_after.shape[1])
    if cfg.embedding_dim_int is not None and cfg.embedding_dim_int != dim:
        raise SystemExit(f"EMBEDDING_DIM={cfg.embedding_dim_int} 与返回维度 {dim} 不符")
    after_doc_matrix = matrix_after[: len(corpus)]
    after_query_matrix = matrix_after[len(corpus) :]

    # before：旧缓存 + content-only（v1）。
    before_texts = [proto_norm(row["text"]) for row in corpus] + query_texts
    matrix_before, before_cache_meta = load_old_matrix(cfg.embedding_model, before_texts)
    before_doc_matrix = matrix_before[: len(corpus)]
    before_query_matrix = matrix_before[len(corpus) :]

    # 直接排序（同一 stable argsort；before 与 rank_metrics.py 历史口径一致）。
    before_vector = [
        rank_matrix(before_doc_matrix, before_query_matrix, i)
        for i in range(len(queries))
    ]
    after_vector = [
        rank_matrix(after_doc_matrix, after_query_matrix, i)
        for i in range(len(queries))
    ]
    con_before = build_bigram_index([row["text"] for row in corpus])
    con_after = build_bigram_index(index_texts)
    before_bigram = [rank_bigram(con_before, item["q"]) for item in queries]
    after_bigram = [rank_bigram(con_after, item["q"]) for item in queries]

    # 产品路径复算 + 逐位对照
    product = run_product_path(corpus, queries, dates, index_texts, client, cfg)
    product_checks = {
        "vector": _compare_order(
            product["vector"], after_vector
        ),
        "bigram": _compare_order(
            product["bigram"], after_bigram
        ),
    }

    per_query: list[dict] = []
    for i, item in enumerate(queries):
        gold = item["gold"]
        per_query.append(
            {
                "sid": item["sid"],
                "q": item["q"],
                "gold": gold,
                "gold_n": len(gold),
                "before_vector": score_ranking(before_vector[i], gold),
                "after_vector": score_ranking(after_vector[i], gold),
                "before_bigram": score_ranking(before_bigram[i], gold),
                "after_bigram": score_ranking(after_bigram[i], gold),
                "product_vector": score_ranking(product["vector"][i], gold),
                "product_bigram": score_ranking(product["bigram"][i], gold),
                "event_times": [list(dates[g]) for g in gold],
                "raw_dates": [corpus[g].get("raw_date") for g in gold],
            }
        )

    vector_before = summarize(per_query, "before_vector")
    vector_after = summarize(per_query, "after_vector")
    bigram_before = summarize(per_query, "before_bigram")
    bigram_after = summarize(per_query, "after_bigram")
    product_vector = summarize(per_query, "product_vector")
    product_bigram = summarize(per_query, "product_bigram")
    bootstrap = {
        metric: paired_bootstrap(
            per_query,
            "after_vector",
            "before_vector",
            metric,
            n=args.bootstrap,
        )
        for metric in ("r1", "r4", "r6", "r20", "mrr")
    }

    # 逐题配对：每个指标的变好/变差/不变；gold 逐条名次变化。
    metric_changes: dict[str, dict] = {}
    rank_changes: list[dict] = []
    for metric in ("r1", "r4", "r6", "r20", "mrr"):
        improved: list[dict] = []
        worsened: list[dict] = []
        unchanged: list[int] = []
        for row in per_query:
            b = row["before_vector"][metric]
            a = row["after_vector"][metric]
            entry = {
                "sid": row["sid"],
                "q": row["q"],
                "before": b,
                "after": a,
                "delta": a - b,
                "before_ranks": row["before_vector"]["gold_ranks"],
                "after_ranks": row["after_vector"]["gold_ranks"],
            }
            if a > b + 1e-12:
                improved.append(entry)
            elif a < b - 1e-12:
                worsened.append(entry)
            else:
                unchanged.append(row["sid"])
        metric_changes[metric] = {
            "improved": improved,
            "worsened": worsened,
            "unchanged_sids": unchanged,
        }
    for row in per_query:
        for gold_index, (before_rank, after_rank) in enumerate(
            zip(row["before_vector"]["gold_ranks"], row["after_vector"]["gold_ranks"])
        ):
            rank_changes.append(
                {
                    "sid": row["sid"],
                    "doc": row["gold"][gold_index],
                    "raw_date": row["raw_dates"][gold_index],
                    "before": before_rank,
                    "after": after_rank,
                    "delta": (
                        None
                        if before_rank is None or after_rank is None
                        else before_rank - after_rank
                    ),
                }
            )

    sid33_index = next(
        (i for i, item in enumerate(queries) if item["sid"] == 33), None
    )
    sid33 = (
        per_query[sid33_index] if sid33_index is not None else None
    )
    sid33_entry = None
    if sid33 is not None:
        def _top4(ranking: list[int]) -> list[dict]:
            return [
                {
                    "doc": doc,
                    "raw_date": corpus[doc].get("raw_date"),
                    "text": corpus[doc]["text"],
                }
                for doc in ranking[:4]
            ]

        sid33_entry = {
            "sid": 33,
            "q": sid33["q"],
            "gold": sid33["gold"],
            "before_vector": sid33["before_vector"],
            "after_vector": sid33["after_vector"],
            "before_bigram": sid33["before_bigram"],
            "after_bigram": sid33["after_bigram"],
            "before_top4": _top4(before_vector[sid33_index]),
            "after_top4": _top4(after_vector[sid33_index]),
            "doc_context": [
                {
                    "doc": doc,
                    "raw_date": corpus[doc].get("raw_date"),
                    "event_time": list(dates[doc]),
                    "text": corpus[doc]["text"],
                }
                for doc in sid33["gold"]
            ],
        }

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "index_text_version": INDEX_TEXT_VERSION,
        "corpus": {
            "docs": len(corpus),
            "queries": len(queries),
            "gold_pairs": sum(len(item["gold"]) for item in queries),
            "eval_clock_ms": FIXED_RECORDED_AT_MS,
            "date_mapping": {
                "year": sum(1 for e, _p in dates if e and len(e) == 4),
                "month": sum(1 for e, _p in dates if e and len(e) == 7),
                "day": sum(1 for e, _p in dates if e and len(e) == 10),
                "fallback_recorded_at": sum(1 for e, _p in dates if e is None),
            },
        },
        "config": {
            "ks": list(KS),
            "route_top_k": ROUTE_TOP_K,
            "eval_final_k": 4,
            "before_index_text": "content（v1）",
            "after_index_text": "date_prefix + '\\n' + content（v2）",
        },
        "metrics": {
            "vector": {
                "before": vector_before,
                "after": vector_after,
                "delta": deltas(vector_after, vector_before),
            },
            "bigram": {
                "before": bigram_before,
                "after": bigram_after,
                "delta": deltas(bigram_after, bigram_before),
            },
            "product_path_vector": product_vector,
            "product_path_bigram": product_bigram,
        },
        "product_path_checks": product_checks,
        "paired_bootstrap_vector_after_minus_before": bootstrap,
        "paired_vector": {
            "metric_changes": metric_changes,
            "rank_changes": rank_changes,
        },
        "sid33": sid33_entry,
        "cache": {
            "old": before_cache_meta,
            "new": {
                "path": str(NEW_CACHE_PATH),
                "model": cfg.embedding_model,
                "dim": dim,
                "texts": len(index_texts) + len(query_texts),
                "this_run": {
                    "cache_hits": client.cache_hits,
                    "new_texts": client.new_texts,
                    "api_calls": client.calls,
                    "prompt_tokens": client.tokens,
                    "tokens_without_usage": client.unpriced,
                    "latency_ms": _latency_stats(client.latencies),
                    "wall_seconds": after_seconds,
                },
                # 首轮构建缓存的实际花费；重跑只命中缓存时 this_run 为 0，这里仍是真实成本。
                "cache_build": {
                    key: value
                    for key, value in client.paid.items()
                    if key != "latencies_ms"
                }
                | {
                    "latency_ms": _latency_stats(
                        list(client.paid.get("latencies_ms") or [])
                    )
                },
            },
        },
        "per_query": per_query,
    }

    Path(args.out).write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    # ---------------------------------------------------------------- stdout
    def _table(name: str, before: dict, after: dict) -> None:
        print(f"\n== {name}（40 题宏平均） ==")
        print(f"{'metric':<8}{'before':>10}{'after':>10}{'delta':>10}")
        for key in ("r1", "r4", "r6", "r20", "mrr", "any_hit4"):
            print(
                f"{key:<8}{before[key]:>10.4f}{after[key]:>10.4f}"
                f"{after[key] - before[key]:>+10.4f}"
            )

    _table("向量单路", vector_before, vector_after)
    _table("二元组单路", bigram_before, bigram_after)
    print(
        f"\n产品路径复算：vector R@4={product_vector['r4']:.4f} "
        f"（直接排序 {vector_after['r4']:.4f}，top50 逐位一致="
        f"{product_checks['vector']['identical_top50']}）；"
        f"bigram top50 逐位一致={product_checks['bigram']['identical_top50']}"
    )
    print(
        f"回归哨兵：v1 向量 R@4={vector_before['r4']:.4f}"
        f"（历史 {BASELINE_R4:.4f}），R@6={vector_before['r6']:.4f}"
        f"（历史 {BASELINE_R6:.4f}）"
    )
    print("\n配对 bootstrap（after − before，40 题同题号重采样 95% CI）：")
    for metric, result in bootstrap.items():
        print(
            f"  Δ{metric:<4}={result['mean']:+.4f} "
            f"[{result['ci95'][0]:+.4f}, {result['ci95'][1]:+.4f}]"
            f"  P(Δ>0)={result['p_gt_zero']:.3f}"
        )
    print("\n逐题配对（向量单路，同一 sid）：")
    for metric in ("r1", "r4", "r6", "r20", "mrr"):
        changes = metric_changes[metric]
        print(
            f"  {metric:<4} 变好 {len(changes['improved'])} / "
            f"变差 {len(changes['worsened'])} / 不变 {len(changes['unchanged_sids'])}"
        )
        for entry in changes["improved"]:
            print(
                f"    + sid {entry['sid']:>2}  {entry['before']:.3f} -> "
                f"{entry['after']:.3f}  名次 {entry['before_ranks']} -> "
                f"{entry['after_ranks']}"
            )
        for entry in changes["worsened"]:
            print(
                f"    - sid {entry['sid']:>2}  {entry['before']:.3f} -> "
                f"{entry['after']:.3f}  名次 {entry['before_ranks']} -> "
                f"{entry['after_ranks']}"
            )
    if sid33_entry:
        print(
            f"\nsid 33：{sid33_entry['q']}\n"
            f"  before 向量名次 {sid33_entry['before_vector']['gold_ranks']}，"
            f"after 向量名次 {sid33_entry['after_vector']['gold_ranks']}\n"
            f"  before R@4={sid33_entry['before_vector']['r4']:.3f}，"
            f"after R@4={sid33_entry['after_vector']['r4']:.3f}"
        )
    cache_build = payload["cache"]["new"]["cache_build"]
    print(
        f"\n花费：首轮构建缓存 {cache_build.get('texts', 0)} 条 / "
        f"{cache_build.get('api_calls', 0)} 次调用 / "
        f"{cache_build.get('prompt_tokens', 0)} input tokens；"
        f"本次运行新算 {client.new_texts} 条 / {client.calls} 次调用 / "
        f"{client.tokens} tokens（缓存命中 {client.cache_hits} 条；"
        f"价格未知，只报实测 token）"
    )
    print(f"结果写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
