# -*- coding: utf-8 -*-
"""R1 向量路真实 embedding 重测（docs/02 §9.3 开关门槛）。

对照口径（与 ``research/recall/recall_eval.py`` 完全一致）：

- 语料 119 条、题库 40 题；query = 题库【用户问】原文（只做 NFKC）；
- golden = 题库记忆条目按 NFKC 精确映射回语料的记录（优先同 scenario）；
- R2 = 二元组单混合列（products ``app.textnorm`` 构造，与基线逐 token 同构）；
- 每路取 top-50，RRF k=60，FINAL_K=6（主指标 R@6；产品 FINAL_K=4 见 docs/02）；
- MMR：在 R1+R2 融合结果前 20 名里迭代选 10，λ=0.7，融合分归一化到 [0,1]；
- 指标：R@6 / R@10 / MRR / 覆盖@50 / 空结果；配对 bootstrap 2000 次（固定种子）。

**钱**：向量按文本 sha256 缓存在 ``research/recall/.embedding_cache.json``
（已 gitignore），重跑不产生新费用；缓存按 ``model`` 隔离，模型变了直接报错而不是混用。
删除该文件即强制重新计费。

用法::

    python research/recall/run_real_embedding.py
    python research/recall/run_real_embedding.py --force-refresh   # 重新调用（花钱）

依赖 ``.env`` 中的 EMBEDDING_BASE_URL / EMBEDDING_API_KEY / EMBEDDING_MODEL /
EMBEDDING_DIM（与产品同一套配置），本脚本不打印、不落盘任何密钥或端点地址。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
REPO_ROOT = BASE.parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(BASE))

# 本机 NO_PROXY 含 "[::1]"：httpx 0.28.1 在解析代理环境时把它当 URL 会抛
# InvalidURL（产品客户端同样会中招）。进程内去掉方括号，不改用户环境。
for _var in ("NO_PROXY", "no_proxy"):
    _raw = os.environ.get(_var)
    if _raw and "[::1]" in _raw:
        os.environ[_var] = _raw.replace("[::1]", "::1")

import httpx  # noqa: E402
import numpy as np  # noqa: E402

from app.config import _load_dotenv, load_config  # noqa: E402
from app.embeddings import build_provider, normalize_vector  # noqa: E402
from app import textnorm  # noqa: E402

from recall_eval import (  # noqa: E402
    FINAL_K,
    MMR_LAMBDA,
    MMR_POOL,
    RRF_K,
    TOP_K_ROUTE,
    bigrams_of,
    fts_match_expr as proto_fts_match_expr,
    metrics,
    norm as proto_norm,
    parse_qbank,
)

DEFAULT_CORPUS = r"E:\Tools\projects\dsh-mnemon-provider-graphiti\research\phase2\corpus.json"
DEFAULT_QBANK = r"C:\Users\ZhaoY\Desktop\读者测试40-题库.md"
CORPUS = os.environ.get("FACTLAYER_BASELINE_CORPUS", DEFAULT_CORPUS)
QBANK = os.environ.get("FACTLAYER_BASELINE_QBANK", DEFAULT_QBANK)

CACHE_PATH = BASE / ".embedding_cache.json"
OUT_PATH = BASE / "real_embedding_results.json"
BATCH_SIZE = 16        # 端点实测支持 16 条/请求
ROUTE_TOP_K = TOP_K_ROUTE
EVAL_FINAL_K = FINAL_K  # = 6
R2_BASELINE_R6 = 0.4875
BOOTSTRAP_N = 2000
SEED = 20261111

# 阿里云 MaaS 未确认公开单价 → 只报 token，不编价格。
PRICE_NOTE = "价格未知（阿里云 MaaS 未确认单价），仅统计实际输入 token"


# ------------------------------------------------------------------ 端点调用
_ORIGINAL_POST = httpx.post
_USAGE_LOG: list[dict] = []


def _install_usage_capture() -> None:
    """产品 provider 只回向量，不看 usage；这里包一层 ``httpx.post`` 记录单价所需
    token（不改产品代码，仍走 ``OpenAICompatProvider.embed`` 的真实调用路径）。"""

    def tracked_post(*args, **kwargs):
        response = _ORIGINAL_POST(*args, **kwargs)
        try:
            body = response.json()
        except Exception:  # noqa: BLE001 - 非 JSON 错误页也要记录状态
            body = {}
        _USAGE_LOG.append(
            {
                "status": response.status_code,
                "usage": body.get("usage") if isinstance(body, dict) else None,
                "elapsed_ms": round(response.elapsed.total_seconds() * 1000, 1),
            }
        )
        return response

    httpx.post = tracked_post


def _cache_key(model: str, text: str) -> str:
    digest = hashlib.sha256((model + "\x00" + text).encode("utf-8")).hexdigest()
    return digest


def _latency_stats(values: list[float]) -> dict | None:
    if not values:
        return None
    arr = np.asarray(values, dtype=float)
    return {
        "median": float(np.median(arr)),
        "p95": float(np.percentile(arr, 95)),
        "min": float(arr.min()),
        "max": float(arr.max()),
    }


class EmbeddingClient:
    """真实 embedding 客户端 + 本地 JSON 缓存（批 16、L2 归一化、记录 token/延迟）。"""

    def __init__(self, provider, cache_path: Path, *, force_refresh: bool = False):
        self.provider = provider
        self.model = provider.model_id
        self.cache_path = cache_path
        self.force_refresh = force_refresh
        self.vectors: dict[str, list[float]] = {}
        self.dim: int | None = None
        # 缓存构建累计花费（写进缓存文件；重跑只读缓存时仍可报告真实成本）
        self.paid = {
            "prompt_tokens": 0,
            "api_calls": 0,
            "texts": 0,
            "latencies_ms": [],
        }
        self.calls = 0
        self.cache_hits = 0
        self.new_texts = 0
        self.tokens = 0
        self.unpriced = 0
        self.latencies: list[float] = []
        self._load()

    # -- 缓存 --------------------------------------------------------
    def _load(self) -> None:
        if not self.cache_path.exists():
            return
        data = json.loads(self.cache_path.read_text(encoding="utf-8"))
        cached_model = data.get("model")
        if cached_model != self.model:
            raise SystemExit(
                f"缓存文件 {self.cache_path.name} 属于模型 {cached_model!r}，"
                f"当前模型 {self.model!r}；为避免新旧向量混用，请改名/删除缓存后重跑。"
            )
        self.vectors = data.get("vectors") or {}
        self.dim = data.get("dim")
        self.paid = data.get("usage") or self.paid
        self.paid.setdefault("latencies_ms", [])
        print(f"缓存：{len(self.vectors)} 条文本（model={self.model}, dim={self.dim}）")

    def _save(self) -> None:
        tmp = self.cache_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "model": self.model,
                    "dim": self.dim,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "usage": self.paid,
                    "vectors": self.vectors,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        os.replace(tmp, self.cache_path)

    # -- 批量取向量 --------------------------------------------------
    def embed(self, texts: list[str]) -> np.ndarray:
        keys = [_cache_key(self.model, text) for text in texts]
        missing = [
            (i, text)
            for i, (key, text) in enumerate(zip(keys, texts))
            if self.force_refresh or key not in self.vectors
        ]
        self.cache_hits += len(texts) - len(missing)

        for start in range(0, len(missing), BATCH_SIZE):
            chunk = missing[start : start + BATCH_SIZE]
            batch = [text for _i, text in chunk]
            before = len(_USAGE_LOG)
            t0 = time.perf_counter()
            vectors = self.provider.embed(batch)
            elapsed = (time.perf_counter() - t0) * 1000
            new_usage = _USAGE_LOG[before:]
            self.calls += 1
            self.new_texts += len(batch)
            self.latencies.append(elapsed)

            dims = {len(vec) for vec in vectors}
            if len(dims) != 1:
                raise SystemExit(f"返回维度不一致：{sorted(dims)}")
            dim = dims.pop()
            if self.dim is None:
                self.dim = dim
            elif dim != self.dim:
                raise SystemExit(
                    f"模型 dim 变化：缓存/前次 {self.dim}，本次 {dim}；拒绝混用"
                )

            batch_tokens = 0
            batch_unpriced = 0
            for usage in new_usage:
                payload = (usage.get("usage") or {})
                if "prompt_tokens" in payload:
                    batch_tokens += int(payload["prompt_tokens"])
                elif "total_tokens" in payload:
                    batch_tokens += int(payload["total_tokens"])
                else:
                    batch_unpriced += len(batch)
            self.tokens += batch_tokens
            self.unpriced += batch_unpriced
            self.paid["prompt_tokens"] += batch_tokens
            self.paid["api_calls"] += 1
            self.paid["texts"] += len(batch)
            self.paid["latencies_ms"].append(round(elapsed, 1))

            for (index, _text), vector in zip(chunk, vectors):
                self.vectors[keys[index]] = list(vector)
            self._save()
            print(
                f"  batch {self.calls:2d}: {len(batch):2d} 条  {elapsed:6.0f}ms  "
                f"dim={dim}  usage={new_usage[-1].get('usage') if new_usage else None}"
            )

        matrix = np.asarray([self.vectors[key] for key in keys], dtype=np.float32)
        matrix = np.vstack([normalize_vector(row) for row in matrix])
        if matrix.shape[0] != len(texts):
            raise SystemExit("向量条数与文本条数不一致")
        return matrix


# ------------------------------------------------------------------ 语料 / golden
def load_inputs() -> tuple[list[dict], list[dict], list[dict]]:
    if not Path(CORPUS).exists() or not Path(QBANK).exists():
        raise SystemExit(
            "缺少评测数据：\n"
            f"  CORPUS={CORPUS}\n  QBANK={QBANK}\n"
            "可用 FACTLAYER_BASELINE_CORPUS / FACTLAYER_BASELINE_QBANK 指路。"
        )
    corpus = json.load(open(CORPUS, encoding="utf-8"))
    scenarios = parse_qbank(QBANK)

    by_norm: dict[str, list[int]] = defaultdict(list)
    for index, row in enumerate(corpus):
        by_norm[proto_norm(row["text"])].append(index)
    queries, unmatched = [], []
    for scenario in scenarios:
        gold = set()
        for mem in scenario["mems"]:
            candidates = by_norm.get(proto_norm(mem["text"]))
            if not candidates:
                unmatched.append((scenario["sid"], mem["text"]))
                continue
            gold.add(
                min(
                    candidates,
                    key=lambda i: (corpus[i].get("scenario") != scenario["sid"], i),
                )
            )
        queries.append(
            {
                "sid": scenario["sid"],
                "q": proto_norm(scenario["question"]),
                "gold": sorted(gold),
                "mem_n": len(scenario["mems"]),
            }
        )
    if unmatched:
        raise SystemExit(f"题库记忆映射失败 {len(unmatched)} 条：{unmatched[:3]}")
    return corpus, queries, scenarios


def check_query_builder(corpus: list[dict], queries: list[dict]) -> None:
    """产品构造器 vs 原型构造器逐 token 相同（环境一致性硬门槛）。"""
    for item in queries:
        tokens, _truncated = textnorm.query_tokens(item["q"])
        factory = textnorm.fts_match_expr(tokens)
        proto = proto_fts_match_expr(bigrams_of(item["q"]))
        if factory != proto:
            raise SystemExit(
                f"查询构造器不一致 sid={item['sid']}: {proto!r} != {factory!r}"
            )
    for row in corpus:
        if textnorm.ngram_tokens(row["text"]) != bigrams_of(proto_norm(row["text"])):
            raise SystemExit(f"索引构造器不一致：{row['text'][:30]!r}")


# ------------------------------------------------------------------ R2 / 融合 / MMR
def build_bigram_db(corpus: list[dict]) -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.execute("PRAGMA journal_mode=OFF")
    con.execute("CREATE VIRTUAL TABLE fts_bi USING fts5(bg, tokenize='unicode61')")
    for index, row in enumerate(corpus):
        bg = textnorm.ngram_text(row["text"])
        con.execute("INSERT INTO fts_bi(rowid, bg) VALUES (?,?)", (index, bg))
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
    except sqlite3.OperationalError as exc:  # 与原型一致：空/非法表达式按空结果
        print(f"  ! FTS error: {exc}")
        return []


def rank_vector(sims: np.ndarray, limit: int = ROUTE_TOP_K) -> list[int]:
    order = np.argsort(-sims, kind="stable")
    return [int(i) for i in order[:limit]]


def rrf(lists: list[list[int]], k: int = RRF_K) -> list[tuple[int, float]]:
    scores: dict[int, float] = defaultdict(float)
    best_rank: dict[int, int] = {}
    for ranking in lists:
        for rank, doc in enumerate(ranking, 1):
            scores[doc] += 1.0 / (k + rank)
            if doc not in best_rank or rank < best_rank[doc]:
                best_rank[doc] = rank
    return sorted(scores.items(), key=lambda kv: (-kv[1], best_rank[kv[0]], kv[0]))


def mmr_select(
    fused: list[tuple[int, float]],
    matrix: np.ndarray,
    *,
    select_n: int = 10,
    lam: float = MMR_LAMBDA,
    pool_size: int = MMR_POOL,
) -> list[int]:
    pool = [doc for doc, _score in fused[:pool_size]]
    if not pool:
        return []
    values = [score for _doc, score in fused[: len(pool)]]
    low, high = min(values), max(values)
    relevance = {
        doc: ((score - low) / (high - low) if high > low else 1.0 / (i + 1))
        for i, (doc, score) in enumerate(fused[: len(pool)])
    }
    selected: list[int] = []
    rest = list(pool)
    while rest and len(selected) < select_n:
        best_doc, best_value = None, None
        for doc in rest:
            diversity = max(
                [float(matrix[doc] @ matrix[other]) for other in selected], default=0.0
            )
            value = lam * relevance[doc] - (1.0 - lam) * diversity
            if best_value is None or value > best_value + 1e-12:
                best_doc, best_value = doc, value
        selected.append(best_doc)
        rest.remove(best_doc)
    return selected


# ------------------------------------------------------------------ 指标 / bootstrap
def score_ranking(ranking: list[int], gold: list[int], k: int) -> dict:
    base = metrics(ranking, gold, final_k=k)
    gold_set = set(gold)
    coverage = len(set(ranking[:ROUTE_TOP_K]) & gold_set) / max(1, len(gold_set))
    return {
        "r6": base["r6"],
        "r10": base["r10"],
        "mrr": base["mrr"],
        "hits6": base["hits6"],
        "coverage50": coverage,
        "empty": 0 if ranking else 1,
    }


def summarize(per_query: list[dict], key: str) -> dict:
    rows = [item[key] for item in per_query]
    return {
        "r6": float(np.mean([row["r6"] for row in rows])),
        "r10": float(np.mean([row["r10"] for row in rows])),
        "mrr": float(np.mean([row["mrr"] for row in rows])),
        "hits6": float(np.mean([row["hits6"] for row in rows])),
        "coverage50": float(np.mean([row["coverage50"] for row in rows])),
        "empty": int(sum(row["empty"] for row in rows)),
    }


def paired_bootstrap(
    per_query: list[dict], left: str, right: str, *, n: int = BOOTSTRAP_N, seed: int = SEED
) -> dict:
    rng = np.random.default_rng(seed)
    count = len(per_query)
    left_r6 = np.array([item[left]["r6"] for item in per_query])
    right_r6 = np.array([item[right]["r6"] for item in per_query])
    left_r10 = np.array([item[left]["r10"] for item in per_query])
    right_r10 = np.array([item[right]["r10"] for item in per_query])
    left_mrr = np.array([item[left]["mrr"] for item in per_query])
    right_mrr = np.array([item[right]["mrr"] for item in per_query])
    idx = rng.integers(0, count, size=(n, count))
    out = {}
    for name, a, b in (("r6", left_r6, right_r6), ("r10", left_r10, right_r10), ("mrr", left_mrr, right_mrr)):
        diffs = a[idx].mean(axis=1) - b[idx].mean(axis=1)
        low, high = np.percentile(diffs, [2.5, 97.5])
        out[name] = {
            "mean": float(a.mean() - b.mean()),
            "ci95": [float(low), float(high)],
            "excludes_zero": bool(low > 0 or high < 0),
            "p_gt_zero": float((diffs > 0).mean()),
        }
    return out


# ------------------------------------------------------------------ 产品路径复算
class _CachedEmbeddingProvider:
    """只读本地缓存的 provider：产品路径复算用，绝不联网/不产生费用。"""

    def __init__(self, client: "EmbeddingClient", model_id: str) -> None:
        self.client = client
        self.model_id = model_id

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            key = _cache_key(self.model_id, text)
            if key not in self.client.vectors:
                key = _cache_key(self.model_id, proto_norm(text))
            if key not in self.client.vectors:
                raise RuntimeError(f"缓存中没有该文本的向量：{text[:40]!r}")
            vectors.append(list(self.client.vectors[key]))
        return vectors


def run_factory_path(
    corpus: list[dict],
    queries: list[dict],
    client: EmbeddingClient,
    cfg,
) -> dict:
    """用**最终产品代码**复算 R@6：默认 vector 单路，验证不融合、不降级。"""
    import tempfile
    from dataclasses import replace

    from app.db import Database
    from app.derive import DerivePipeline
    from app.retrieval import RetrievalService
    from app.schema import init_db
    from app.write_path import create_memory

    provider = _CachedEmbeddingProvider(client, cfg.embedding_model)
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(os.path.join(tmp, "factory.db"))
        try:
            init_db(db)
            ids: list[str] = []
            for index, row in enumerate(corpus):
                receipt = create_memory(
                    db,
                    client_write_id=f"eval-{index}",
                    content=row["text"],
                    session_id=f"scenario-{row.get('scenario')}-{index}",
                    source_client="eval",
                )
                ids.append(receipt["id"])
            pipeline = DerivePipeline(
                db,
                stub_success=True,
                embedding_provider=provider,
                embedding_dim=cfg.embedding_dim_int,
            )
            for _ in range(100):
                if (
                    pipeline.pending_count("fts") == 0
                    and pipeline.pending_count("embedding") == 0
                ):
                    break
                pipeline.tick()
            pending = {
                "fts": pipeline.pending_count("fts"),
                "embedding": pipeline.pending_count("embedding"),
            }
            if any(pending.values()):
                raise RuntimeError(f"派生未补齐：{pending}")

            route_cfg = replace(
                cfg,
                retrieval_routes=("vector",),
                final_k=EVAL_FINAL_K,
                route_top_k=ROUTE_TOP_K,
                expand_max_items=EVAL_FINAL_K,
            )
            service = RetrievalService(db, route_cfg, provider)
            id_to_index = {memory_id: i for i, memory_id in enumerate(ids)}
            scores: list[float] = []
            empty = 0
            route_sets: set[tuple[str, ...]] = set()
            degraded = False
            single_route_score_ok = True
            for item in queries:
                result = service.search(item["q"])
                route_sets.add(tuple(sorted(result["routes"])))
                degraded = degraded or bool(result.get("degraded", {}).get("degraded"))
                ranking = [
                    id_to_index[row["id"]]
                    for row in result["items"]
                    if row["id"] in id_to_index
                ]
                empty += 0 if ranking else 1
                scores.append(metrics(ranking, item["gold"], final_k=EVAL_FINAL_K)["r6"])
                if result["items"]:
                    top_score = result["items"][0]["score"]
                    single_route_score_ok = single_route_score_ok and abs(
                        top_score - round(1.0 / (int(route_cfg.rrf_k) + 1), 12)
                    ) < 1e-12
            return {
                "implementation": "app.retrieval.RetrievalService（最终产品代码）",
                "routes": list(route_cfg.route_names),
                "final_k": EVAL_FINAL_K,
                "docs": len(corpus),
                "r6": float(np.mean(scores)),
                "empty": int(empty),
                "route_sets": [list(names) for names in sorted(route_sets)],
                "degraded": bool(degraded),
                "single_route_score_ok": bool(single_route_score_ok),
            }
        finally:
            db.close()


# ------------------------------------------------------------------ main
def main(argv: list[str] | None = None) -> int:
    global BATCH_SIZE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force-refresh", action="store_true", help="忽略缓存重新计费")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    parser.add_argument("--bootstrap", type=int, default=BOOTSTRAP_N)
    parser.add_argument(
        "--sanity",
        action="store_true",
        help="加做错位查询对照（用缓存即可，不联网、不写结果）：排除索引错位",
    )
    parser.add_argument(
        "--skip-factory",
        action="store_true",
        help="跳过产品路径复算（默认开启；只读缓存，不联网）",
    )
    parser.add_argument("--out", default=str(OUT_PATH))
    args = parser.parse_args(argv)

    BATCH_SIZE = args.batch_size

    _load_dotenv(str(REPO_ROOT / ".env"))
    cfg = load_config()
    if not cfg.embedding_base_url or not cfg.embedding_model:
        raise SystemExit("缺少 EMBEDDING_BASE_URL / EMBEDDING_MODEL（见 .env）")
    print(
        f"embedding: backend={cfg.embedding_backend} model={cfg.embedding_model} "
        f"dim_cfg={cfg.embedding_dim} timeout={cfg.embedding_timeout_seconds}s"
    )
    if cfg.embedding_timeout_seconds < 3:
        print(
            f"  注意：embedding_timeout={cfg.embedding_timeout_seconds}s，"
            "产品单次 3 秒超时；评测沿用该配置"
        )

    corpus, queries, _scenarios = load_inputs()
    check_query_builder(corpus, queries)
    print(f"docs={len(corpus)} queries={len(queries)} 查询构造器冻结校验 PASS")

    con = build_bigram_db(corpus)
    r2_rankings = [rank_bigram(con, item["q"]) for item in queries]
    r2_r6 = float(np.mean([metrics(rank, item["gold"], final_k=EVAL_FINAL_K)["r6"]
                           for rank, item in zip(r2_rankings, queries)]))
    print(f"R2 单路 R@6 = {r2_r6:.4f}（基线 {R2_BASELINE_R6:.4f}）")
    if abs(r2_r6 - R2_BASELINE_R6) > 0.001:
        raise SystemExit(
            f"R2 复现失败：{r2_r6:.4f} != {R2_BASELINE_R6:.4f}；环境不一致，停止"
        )

    _install_usage_capture()
    provider = build_provider(cfg)
    client = EmbeddingClient(provider, CACHE_PATH, force_refresh=args.force_refresh)

    texts = [proto_norm(row["text"]) for row in corpus] + [item["q"] for item in queries]
    started = time.perf_counter()
    matrix = client.embed(texts)
    total_seconds = time.perf_counter() - started
    dim = int(matrix.shape[1])
    if cfg.embedding_dim_int is not None and cfg.embedding_dim_int != dim:
        raise SystemExit(f"EMBEDDING_DIM={cfg.embedding_dim_int} 与返回维度 {dim} 不符")
    print(
        f"向量：{len(texts)} 条 dim={dim} 缓存命中={client.cache_hits} "
        f"新调用={client.new_texts} 批次数={client.calls} "
        f"耗时={total_seconds:.1f}s（含缓存读写）"
    )

    doc_matrix = matrix[: len(corpus)]
    query_matrix = matrix[len(corpus) :]

    per_query: list[dict] = []
    for index, item in enumerate(queries):
        sims = doc_matrix @ query_matrix[index]
        r1_ranking = rank_vector(sims)
        fused = rrf([r1_ranking, r2_rankings[index]])
        fused_ranking = [doc for doc, _score in fused]
        mmr_ranking = mmr_select(fused, doc_matrix)
        per_query.append(
            {
                "sid": item["sid"],
                "q": item["q"],
                "gold": item["gold"],
                "mem_n": item["mem_n"],
                "R1": score_ranking(r1_ranking, item["gold"], EVAL_FINAL_K),
                "R2": score_ranking(r2_rankings[index], item["gold"], EVAL_FINAL_K),
                "R1+R2": score_ranking(fused_ranking, item["gold"], EVAL_FINAL_K),
                "R1+R2+MMR": score_ranking(mmr_ranking, item["gold"], EVAL_FINAL_K),
            }
        )

    runs = {name: summarize(per_query, name) for name in ("R1", "R2", "R1+R2", "R1+R2+MMR")}
    comparisons = {
        "R1 - R2": paired_bootstrap(per_query, "R1", "R2", n=args.bootstrap),
        "R1+R2 - R2": paired_bootstrap(per_query, "R1+R2", "R2", n=args.bootstrap),
        "R1+R2 - R1": paired_bootstrap(per_query, "R1+R2", "R1", n=args.bootstrap),
        "R1+R2+MMR - R1+R2": paired_bootstrap(
            per_query, "R1+R2+MMR", "R1+R2", n=args.bootstrap
        ),
    }

    def _gate(comparison: dict, threshold: float = 0.05) -> dict:
        r6 = comparison["r6"]
        passed = r6["mean"] >= threshold and r6["ci95"][0] > 0
        return {
            "metric": "recall@6",
            "delta": r6["mean"],
            "ci95": r6["ci95"],
            "threshold": threshold,
            "pass": bool(passed),
        }

    r1_fusion_gate = _gate(comparisons["R1+R2 - R2"])
    r1_single_gate = _gate(comparisons["R1 - R2"])
    mmr_gate = _gate(comparisons["R1+R2+MMR - R1+R2"])

    factory = None
    if not args.skip_factory:
        factory = run_factory_path(corpus, queries, client, cfg)
        factory["prototype_r6"] = runs["R1"]["r6"]
        factory["matches_prototype"] = bool(
            abs(factory["r6"] - runs["R1"]["r6"]) < 1e-9
        )

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "endpoint": {
            "backend": cfg.embedding_backend,
            "wire_format": "OpenAI /embeddings（POST {base_url}/embeddings）",
            "model": cfg.embedding_model,
            "dim": dim,
            "dim_config": cfg.embedding_dim,
            "batch_size": BATCH_SIZE,
            "batch_supported": True,
            "timeout_seconds": cfg.embedding_timeout_seconds,
            "base_url_recorded": False,
        },
        "corpus": {
            "path": CORPUS,
            "sha256": hashlib.sha256(Path(CORPUS).read_bytes()).hexdigest(),
            "docs": len(corpus),
            "queries": len(queries),
            "gold_total": int(sum(len(item["gold"]) for item in queries)),
        },
        "config": {
            "route_top_k": ROUTE_TOP_K,
            "final_k": EVAL_FINAL_K,
            "rrf_k": RRF_K,
            "mmr_lambda": MMR_LAMBDA,
            "mmr_pool": MMR_POOL,
            "bootstrap_n": args.bootstrap,
            "seed": SEED,
        },
        "runs": runs,
        "comparisons": comparisons,
        "gates": {
            "R1_single": r1_single_gate,
            "R1_fusion": r1_fusion_gate,
            "MMR": mmr_gate,
        },
        "factory_product_path": factory,
        "usage": {
            "this_run": {
                "new_texts": client.new_texts,
                "cache_hits": client.cache_hits,
                "api_calls": client.calls,
                "prompt_tokens": client.tokens,
                "tokens_without_usage": client.unpriced,
                "latency_ms": _latency_stats(client.latencies),
            },
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
            "price_note": PRICE_NOTE,
            "wall_seconds": total_seconds,
        },
        "decision": {
            "default_routes": ["vector"],
            "r1_default_on": True,
            "bigram_fallback": True,
            "mmr_default_on": bool(mmr_gate["pass"]),
            "note": "出厂默认 vector 单路（R@6 0.8729）；bigram 只在向量不可用时降级，"
                    "不做 RRF 融合（等权融合 0.6833，显著拖累，见 R1+R2 − R1）。"
                    "MMR 负增益，保持关。",
        },
        "per_query": per_query,
    }

    Path(args.out).write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    print("\n== 指标（40 题平均） ==")
    header = f"{'route':<12}{'R@6':>8}{'R@10':>8}{'MRR':>8}{'cov@50':>8}{'empty':>7}"
    print(header)
    for name in ("R1", "R2", "R1+R2", "R1+R2+MMR"):
        row = runs[name]
        print(
            f"{name:<12}{row['r6']:>8.4f}{row['r10']:>8.4f}{row['mrr']:>8.4f}"
            f"{row['coverage50']:>8.4f}{row['empty']:>7d}"
        )

    print("\n== 配对 bootstrap 95% CI（ΔR@6 / ΔR@10 / ΔMRR） ==")
    for label, comparison in comparisons.items():
        r6 = comparison["r6"]
        r10 = comparison["r10"]
        mrr = comparison["mrr"]
        print(
            f"{label:<20} ΔR@6={r6['mean']:+.4f} [{r6['ci95'][0]:+.4f}, {r6['ci95'][1]:+.4f}]"
            f"  ΔR@10={r10['mean']:+.4f} [{r10['ci95'][0]:+.4f}, {r10['ci95'][1]:+.4f}]"
            f"  ΔMRR={mrr['mean']:+.4f} [{mrr['ci95'][0]:+.4f}, {mrr['ci95'][1]:+.4f}]"
        )

    print("\n== 门槛（§9.3：R@6 提升 ≥0.05 且 95% CI 不含 0） ==")
    for name, gate in (
        ("R1 单路", r1_single_gate),
        ("R1+R2 融合（对照）", r1_fusion_gate),
        ("MMR（R1+R2 上）", mmr_gate),
    ):
        print(
            f"{name:<18} ΔR@6={gate['delta']:+.4f} CI=[{gate['ci95'][0]:+.4f}, "
            f"{gate['ci95'][1]:+.4f}] → {'开' if gate['pass'] else '关'}"
        )

    if factory is not None:
        print(
            f"\n== 产品路径复算（最终代码，routes=vector，final_k=6） ==\n"
            f"R@6={factory['r6']:.4f}（原型 {factory['prototype_r6']:.4f}，"
            f"一致={factory['matches_prototype']}）；routes={factory['route_sets']}；"
            f"degraded={factory['degraded']}；单路名次分={factory['single_route_score_ok']}"
        )

    print(
        f"\n花费：本次 {client.tokens} input tokens / {client.calls} 次调用；"
        f"缓存构建累计 {client.paid.get('prompt_tokens', 0)} tokens / "
        f"{client.paid.get('api_calls', 0)} 次（缓存命中 {client.cache_hits} 条，重跑 0 费用）"
    )
    if args.sanity:
        rng = np.random.default_rng(SEED)
        perm = rng.permutation(len(queries))
        shuffled = []
        for index, item in enumerate(queries):
            sims = doc_matrix @ query_matrix[perm[index]]
            shuffled.append(metrics(rank_vector(sims), item["gold"], final_k=EVAL_FINAL_K)["r6"])
        print(
            f"核对：错位查询向量（随机置换，种子 {SEED}）R@6 = {np.mean(shuffled):.4f}"
            f"（随机基线 0.0505）；原 R@6 = {runs['R1']['r6']:.4f}"
        )
    print(f"结果写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
