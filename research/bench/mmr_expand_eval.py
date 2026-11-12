# -*- coding: utf-8 -*-
"""MMR 与会话展开的**检索侧**验证（LongMemEval oracle 语料 337 题，生产口径 R@4）。

问题
----
产品默认 ``RETRIEVAL_ROUTES=vector`` 单路、只回 4 条（``FINAL_K=4``）。两个候选
特性都**没有在真实语料上验证过**：

- **MMR**：未实现；内部 40 题小语料上用真实 embedding 测到负增益
  （R@6 −0.0667，95% CI [−0.1167, −0.0207]，[docs/04](../docs/04-embedding重测报告.md)）；
- **会话展开**：``app/retrieval.py:_expand`` 已实现、默认关（``EXPAND_ENABLED=0``，
  ``EXPAND_WINDOW=1``），从未验证。

本脚本在 ``research/bench/converted/longmemeval_clean.jsonl``（337 题 / 7,419 条
oracle 事实 / 643 个会话）上，用**产品同款** embedding provider 与索引文本比较
四种配置：

1. ``baseline``：余弦 top-50 → 取前 4；
2. ``mmr``：在 top-50 上做迭代 MMR（``λ=0.7``，余弦作多样性度量）得到完整 50 序，
   取前 4（R@10/R@20 取该序前缀）；
3. ``expand``：baseline 取 top-6 作种子（模拟产品取 4 后为展开多取 2 条），把命中
   项**同 session ±1 条**邻居加入结果集（不占 4 条名额），再算
   ``evidence-in-set@4+expand``（gold 是否在 ``top-4 ∪ 邻居`` 里）；
4. ``mmr_expand``：MMR top-6 作种子 + MMR top-4，同口径。

指标
----
主口径 ``hit@k``：任一 gold 证据在 top-k（二元，逐题 0/1，与"证据在不在结果集里"
同义）；次口径 ``cov@k``：gold 覆盖比例（与 ``research/recall`` 历史口径一致）。
展开配置用 ``evidence-in-set@4+expand``；配对 bootstrap 2000 次给 Δ 的 95% CI。
**启用判据：Δ 的 95% CI 下界 > 0。**

分组：单 gold 题 vs 多 gold 题（一题多条证据）——MMR 的多样性惩罚理论上更容易
压掉同会话的多条 gold，这一组要单独看；同时给出剔除 abstention（30 题，gold 是
整段"无答案"会话）的 answerable 子集。

钱 / 缓存
--------
只调 embedding（facts + 337 个 question），**不调 reader / judge**。所有向量缓存到
``.embedding_cache_mmr.json``（已 gitignore），复跑 0 调用。端点报错/余额不足 →
立即停下并报告，**不换模型重试**。

索引文本用**产品口径** ``index_text_for``（v2：日期前缀 + 正文，docs/09 实测在内部
语料上 R@4 +0.0271）；查询文本用 ``textnorm.normalize``（与 ``retrieval.py`` 向量路
一致）。四种配置共用同一批向量，配置间 Δ 不受索引文本选择影响。

用法::

    python research/bench/mmr_expand_eval.py --smoke      # 前 3 题冒烟
    python research/bench/mmr_expand_eval.py              # 全量 337 题
    python research/bench/mmr_expand_eval.py --offline    # 只读缓存复算（0 调用）
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent
REPO_ROOT = BASE.parent.parent
sys.path.insert(0, str(REPO_ROOT))

# 与 research/recall/run_real_embedding.py 相同的本机畸形 NO_PROXY 兼容处理
# （``[::1]`` 会让 httpx 在客户端构造阶段抛 InvalidURL）。
for _var in ("NO_PROXY", "no_proxy"):
    _raw = os.environ.get(_var)
    if _raw and "[::1]" in _raw:
        os.environ[_var] = _raw.replace("[::1]", "::1")

from app import textnorm  # noqa: E402
from app.config import _load_dotenv, load_config  # noqa: E402
from app.embeddings import build_provider, normalize_vector  # noqa: E402
from app.index_text import index_text_for  # noqa: E402

DATA_PATH = BASE / "converted" / "longmemeval_clean.jsonl"
CACHE_PATH = BASE / ".embedding_cache_mmr.json"
OUT_PATH = BASE / "mmr_expand_eval.json"
SMOKE_OUT_PATH = BASE / "mmr_expand_eval.smoke.json"

# ---- 检索/实验参数（与产品默认对齐，见 app/config.py） -------------------
ROUTE_TOP_K = 50          # 候选池：余弦 top-50（= ROUTE_TOP_K）
FINAL_K = 4               # 产品只回 4 条（FINAL_K）
MMR_LAMBDA = 0.7          # 任务指定
MMR_POOL = ROUTE_TOP_K    # 在 top-50 上迭代选（任务指定；原型是融合池前 20）
EXPAND_SEED_K = 6         # 展开种子：模拟产品取 4 后多取 2 条
EXPAND_WINDOW = 1         # ±1 条
KS = (1, 4, 10, 20)       # R@1 / R@4 / R@10 / R@20
BOOTSTRAP_N = 2000
SEED = 20261212

# λ / 池大小 / 窗口 敏感性（全部不花 embedding 调用）
LAMBDA_SWEEP = (0.3, 0.5, 0.6, 0.7, 0.8, 0.9)
POOL_SWEEP = (20, 50)
WINDOW_SWEEP = (1, 2)

BATCH_SIZE = 16           # 与 research/recall 端点实测一致（16 条/请求）
EMBED_WORKERS = 6
MAX_ATTEMPTS = 3
RETRY_DELAYS = (1.5, 4.0)
CACHE_SCHEMA = 1


# =========================================================== 缓存 + 端点调用
def cache_key(model: str, text: str) -> str:
    return hashlib.sha256((model + "\x00" + text).encode("utf-8")).hexdigest()


def _pack(vector) -> str:
    arr = np.asarray(normalize_vector(vector), dtype="<f4")
    return base64.b64encode(arr.tobytes()).decode("ascii")


def _unpack(encoded: str, dim: int | None = None) -> np.ndarray:
    arr = np.frombuffer(base64.b64decode(encoded.encode("ascii")), dtype="<f4")
    if dim is not None and arr.size != dim:
        raise SystemExit(f"缓存向量维度 {arr.size} 与期望 {dim} 不符")
    return arr.astype(np.float32, copy=False)


# 产品 provider 只回向量，不看 usage；包一层 ``httpx.Client.post`` 记录真实
# prompt_tokens（不改 app/，仍走 OpenAICompatProvider.embed 的真实调用路径）。
_ORIGINAL_CLIENT_POST = None
_TLS = threading.local()


def install_usage_capture() -> None:
    global _ORIGINAL_CLIENT_POST
    if _ORIGINAL_CLIENT_POST is not None:
        return
    import httpx

    original = httpx.Client.post

    def tracked(self, *args, **kwargs):
        response = original(self, *args, **kwargs)
        bucket = getattr(_TLS, "bucket", None)
        if bucket is not None:
            try:
                body = response.json()
            except Exception:  # noqa: BLE001 - 非 JSON 错误页也要记录状态
                body = {}
            bucket.append(
                {
                    "status": response.status_code,
                    "usage": body.get("usage") if isinstance(body, dict) else None,
                    "elapsed_ms": round(response.elapsed.total_seconds() * 1000, 1),
                }
            )
        return response

    httpx.Client.post = tracked
    _ORIGINAL_CLIENT_POST = original


class EmbeddingAbort(SystemExit):
    """端点失败/余额不足：按约定停下，绝不换模型重试。"""


class EmbeddingStore:
    """真实 provider + JSON 缓存（base64 float32、批量 16、L2 归一化、可续跑）。

    缓存文件结构::

        {"schema": 1, "model": ..., "dim": ..., "encoding": "f32-b64",
         "created_at": ..., "usage": {...}, "vectors": {sha256: "<base64 f32>"}}

    存的是**已 L2 归一化**的 float32（与产品写库口径一致），点积即余弦。
    """

    def __init__(self, path: Path, provider, *, offline: bool = False) -> None:
        self.path = path
        self.provider = provider
        self.model = provider.model_id
        self.offline = offline
        self.vectors: dict[str, str] = {}
        self.dim: int | None = None
        self.usage = {"prompt_tokens": 0, "api_calls": 0, "texts": 0, "latencies_ms": []}
        self.cache_hits = 0
        self.new_texts = 0
        self.calls = 0
        self.tokens = 0
        self._lock = threading.Lock()
        self._dirty = 0
        self._last_save = 0.0
        self._load()

    # -- IO ---------------------------------------------------------
    def _load(self) -> None:
        if not self.path.exists():
            return
        data = json.loads(self.path.read_text(encoding="utf-8"))
        cached_model = data.get("model")
        if cached_model != self.model:
            raise SystemExit(
                f"缓存 {self.path.name} 属于模型 {cached_model!r}，当前 {self.model!r}；"
                "为避免新旧向量混用，请改名/删除缓存后重跑。"
            )
        if data.get("encoding") != "f32-b64":
            raise SystemExit(f"缓存编码 {data.get('encoding')!r} 不受支持")
        self.vectors = data.get("vectors") or {}
        self.dim = data.get("dim")
        self.usage = data.get("usage") or self.usage
        self.usage.setdefault("latencies_ms", [])
        print(f"缓存：{len(self.vectors)} 条文本（model={self.model}, dim={self.dim}）")

    def _save(self) -> None:
        payload = {
            "schema": CACHE_SCHEMA,
            "encoding": "f32-b64",
            "model": self.model,
            "dim": self.dim,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "usage": self.usage,
            "vectors": self.vectors,
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)
        self._dirty = 0
        self._last_save = time.monotonic()

    def _maybe_save(self, force: bool = False) -> None:
        if not force and self._dirty < 96 and time.monotonic() - self._last_save < 20:
            return
        with self._lock:
            if self._dirty == 0:
                return
            self._save()

    # -- 端点 -------------------------------------------------------
    def _embed_batch(self, chunk: list[tuple[str, str]]) -> tuple[list[str], list]:
        texts = [text for _key, text in chunk]
        last_error: BaseException | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            _TLS.bucket = []
            started = time.perf_counter()
            try:
                vectors = self.provider.embed(texts)
            except Exception as exc:  # noqa: BLE001 - 统一分类后重试/中止
                last_error = exc
                if attempt < MAX_ATTEMPTS:
                    time.sleep(RETRY_DELAYS[attempt - 1])
                    continue
                raise EmbeddingAbort(
                    "embedding 端点调用失败（已重试 "
                    f"{MAX_ATTEMPTS} 次），按约定停止、不换模型：{type(exc).__name__}: {exc}"
                ) from exc
            elapsed_ms = (time.perf_counter() - started) * 1000
            dims = {len(vec) for vec in vectors}
            if len(dims) != 1:
                raise SystemExit(f"embedding 返回维度不一致：{sorted(dims)}")
            dim = dims.pop()
            if self.dim is None:
                self.dim = dim
            elif dim != self.dim:
                raise SystemExit(f"模型 dim 变化：{self.dim} → {dim}；拒绝混用")
            tokens = 0
            for entry in _TLS.bucket:
                usage = entry.get("usage") or {}
                if "prompt_tokens" in usage:
                    tokens += int(usage["prompt_tokens"])
                elif "total_tokens" in usage:
                    tokens += int(usage["total_tokens"])
            return [key for key, _text in chunk], vectors, tokens, elapsed_ms
        raise AssertionError(last_error)  # pragma: no cover

    def ensure(self, texts: list[str]) -> None:
        """确保这些文本的向量在缓存里；缺的并发补齐（batch 16）。"""
        keys = [cache_key(self.model, text) for text in texts]
        missing: dict[str, str] = {}
        for key, text in zip(keys, texts):
            if key not in self.vectors and key not in missing:
                missing[key] = text
        self.cache_hits += len(texts) - len(missing)
        if not missing:
            print(f"缓存命中：{len(texts)}/{len(texts)} 条，0 调用")
            return
        if self.offline:
            raise SystemExit(
                f"离线模式但缓存缺 {len(missing)} 条文本（示例 {next(iter(missing.values()))[:60]!r}）"
            )
        items = list(missing.items())
        batches = [
            items[start : start + BATCH_SIZE] for start in range(0, len(items), BATCH_SIZE)
        ]
        print(
            f"需要新算 {len(missing)} 条文本（{len(batches)} 批 × {BATCH_SIZE}，"
            f"并发 {EMBED_WORKERS}）"
        )
        self._last_save = time.monotonic()
        done = 0
        with ThreadPoolExecutor(max_workers=EMBED_WORKERS) as pool:
            futures = [pool.submit(self._embed_batch, batch) for batch in batches]
            try:
                for future in as_completed(futures):
                    keys_done, vectors, tokens, elapsed_ms = future.result()
                    with self._lock:
                        for key, vector in zip(keys_done, vectors):
                            self.vectors[key] = _pack(vector)
                        self.calls += 1
                        self.new_texts += len(keys_done)
                        self.tokens += tokens
                        self.usage["prompt_tokens"] += tokens
                        self.usage["api_calls"] += 1
                        self.usage["texts"] += len(keys_done)
                        self.usage["latencies_ms"].append(round(elapsed_ms, 1))
                        self._dirty += len(keys_done)
                    done += len(keys_done)
                    if done % 160 == 0 or done == len(missing):
                        print(
                            f"  {done:5d}/{len(missing)} 条  tokens={self.tokens}  "
                            f"calls={self.calls}"
                        )
                    self._maybe_save()
            except BaseException:
                for pending in futures:
                    pending.cancel()
                self._maybe_save(force=True)
                raise
        self._maybe_save(force=True)
        print(
            f"新算完成：{self.new_texts} 条 / {self.calls} 次调用 / "
            f"{self.tokens} prompt tokens"
        )

    def matrix(self, texts: list[str], *, check: bool = True) -> np.ndarray:
        rows: list[np.ndarray] = []
        for text in texts:
            key = cache_key(self.model, text)
            encoded = self.vectors.get(key)
            if encoded is None:
                if check:
                    raise SystemExit(f"缓存缺向量：{text[:60]!r}")
                encoded = ""
            rows.append(_unpack(encoded, self.dim) if encoded else np.zeros(self.dim or 0))
        if not rows:
            return np.zeros((0, self.dim or 0), dtype=np.float32)
        return np.vstack(rows).astype(np.float32, copy=False)


# =========================================================== 语料 / 文本
def load_dataset(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def fact_index_text(fact: dict) -> str:
    """事实 → 产品索引文本 v2（日期前缀 + 正文）；日期缺失则退化为正文。"""
    return index_text_for(
        {
            "content": fact.get("text") or "",
            "event_time": fact.get("date"),
            "event_time_precision": None,
            "recorded_at": None,
        }
    )


def question_text(row: dict) -> str:
    """查询文本：与 retrieval.py 向量路同一构造函数。"""
    return textnorm.normalize(row["question"])


# =========================================================== 排序 / MMR / 展开
def cosine_order(sims: np.ndarray, limit: int) -> list[int]:
    return [int(i) for i in np.argsort(-sims, kind="stable")[:limit]]


def cosine_matrix(matrix: np.ndarray) -> np.ndarray:
    """归一化行 → 余弦矩阵（float32 点积）。"""
    return matrix @ matrix.T


def mmr_order(
    pool: list[int],
    pair_sim: np.ndarray,
    relevance: np.ndarray,
    *,
    lam: float,
    select_n: int,
) -> list[int]:
    """在 ``pool``（已按相关性降序）上迭代 MMR，返回完整选择序（长度 ≤ select_n）。

    - 相关性：池内余弦做 min-max 归一化（与 ``research/recall`` 原型一致）；
    - 多样性：与已选项的最大余弦（候选向量都已 L2 归一化）。
    """
    size = len(pool)
    if size == 0:
        return []
    rel_vals = relevance[np.asarray(pool)]
    low, high = float(rel_vals.min()), float(rel_vals.max())
    if high > low:
        rel = (rel_vals - low) / (high - low)
    else:  # 全等分：退化为名次先验
        rel = np.array([1.0 / (i + 1) for i in range(size)], dtype=np.float32)
    order: list[int] = []
    rest = np.ones(size, dtype=bool)
    chosen: list[int] = []
    for _ in range(min(select_n, size)):
        if chosen:
            diversity = pair_sim[:, chosen].max(axis=1)
        else:
            diversity = np.zeros(size, dtype=np.float32)
        scores = lam * rel - (1.0 - lam) * diversity
        scores[~rest] = -np.inf
        best = int(np.argmax(scores))
        order.append(pool[best])
        rest[best] = False
        chosen.append(best)
    return order


def expand_neighbors(
    facts: list[dict],
    by_session: dict[str, list[tuple[int, int]]],
    seeds: list[int],
    already: set[int],
    window: int,
) -> list[int]:
    """同 session ±window 条邻居（不占名额）；按 seed 名次、同会话按 turn 排序。"""
    seen = set(already)
    out: list[int] = []
    for seed in seeds:
        fact = facts[seed]
        session = fact.get("session_id")
        turn = int(fact.get("turn_index") or 0)
        for other_turn, other in by_session.get(session, ()):
            if other == seed or other in seen:
                continue
            if abs(other_turn - turn) <= window:
                seen.add(other)
                out.append(other)
    return out


# =========================================================== 指标
def hit_at(ranking: list[int], gold: set[int], k: int) -> int:
    return int(any(doc in gold for doc in ranking[:k]))


def cov_at(ranking: list[int], gold: set[int], k: int) -> float:
    if not gold:
        return 0.0
    return len(set(ranking[:k]) & gold) / len(gold)


def first_gold_rank(ranking: list[int], gold: set[int]) -> int | None:
    for rank, doc in enumerate(ranking, 1):
        if doc in gold:
            return rank
    return None


def evaluate_question(
    row: dict,
    fact_texts: list[str],
    query: str,
    store: EmbeddingStore,
    *,
    lam: float = MMR_LAMBDA,
    pool_size: int = MMR_POOL,
    windows: tuple[int, ...] = (EXPAND_WINDOW,),
) -> dict:
    facts = row["facts"]
    n = len(facts)
    gold_ids = set(row["gold_evidence_fact_ids"])
    gold = {i for i, fact in enumerate(facts) if fact["fact_id"] in gold_ids}
    assert gold, f"{row['id']} 没有 gold 证据"

    matrix = store.matrix(fact_texts)
    query_vec = store.matrix([query])[0]
    sims = matrix @ query_vec
    base_order = cosine_order(sims, min(ROUTE_TOP_K, n))
    pool = base_order[: min(pool_size, len(base_order))]
    pool_matrix = matrix[np.asarray(pool)]
    pair_sim = cosine_matrix(pool_matrix)

    record: dict = {
        "qid": row["id"],
        "category": row.get("category"),
        "abstention": bool(row.get("is_abstention")),
        "n_facts": n,
        "n_gold": len(gold),
        "gold_rank": first_gold_rank(base_order, gold),
    }

    # ---- baseline --------------------------------------------------
    baseline = {
        "hit1": hit_at(base_order, gold, 1),
        "hit4": hit_at(base_order, gold, 4),
        "hit10": hit_at(base_order, gold, 10),
        "hit20": hit_at(base_order, gold, 20),
        "cov4": cov_at(base_order, gold, 4),
        "cov10": cov_at(base_order, gold, 10),
        "cov20": cov_at(base_order, gold, 20),
    }
    rank = first_gold_rank(base_order, gold)
    baseline["mrr"] = (1.0 / rank) if rank else 0.0
    record["baseline"] = baseline

    # ---- MMR（完整 50 序，R@k 取前缀） -----------------------------
    mmr_full = mmr_order(
        pool, pair_sim, sims, lam=lam, select_n=len(pool)
    )
    rank = first_gold_rank(mmr_full, gold)
    record["mmr"] = {
        "hit1": hit_at(mmr_full, gold, 1),
        "hit4": hit_at(mmr_full, gold, 4),
        "hit10": hit_at(mmr_full, gold, 10),
        "hit20": hit_at(mmr_full, gold, 20),
        "cov4": cov_at(mmr_full, gold, 4),
        "cov10": cov_at(mmr_full, gold, 10),
        "cov20": cov_at(mmr_full, gold, 20),
        "mrr": (1.0 / rank) if rank else 0.0,
    }

    # ---- 会话展开 ---------------------------------------------------
    by_session: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for pos, fact in enumerate(facts):
        by_session[fact.get("session_id")].append(
            (int(fact.get("turn_index") or 0), pos)
        )
    for session in by_session.values():
        session.sort()

    def expand_metrics(ranking: list[int], seed_k: int, window: int) -> dict:
        returned = ranking[:4]
        seeds = ranking[: min(seed_k, len(ranking))]
        neighbors = expand_neighbors(
            facts, by_session, seeds, set(returned), window
        )
        final_set = set(returned) | set(neighbors)
        return {
            "hit_set": int(bool(final_set & gold)),
            "cov_set": (len(final_set & gold) / len(gold)) if gold else 0.0,
            "set_size": len(final_set),
            "n_neighbors": len(neighbors),
            "recovered": int(bool(final_set & gold)) - hit_at(returned, gold, 4),
            "seed6_has_gold": hit_at(seeds, gold, len(seeds)),
        }

    record["expand"] = expand_metrics(base_order, EXPAND_SEED_K, EXPAND_WINDOW)
    record["mmr_expand"] = expand_metrics(mmr_full, EXPAND_SEED_K, EXPAND_WINDOW)

    # ---- 敏感性（不花调用） -----------------------------------------
    sens: dict = {}
    # 1) 展开种子只取 top-4（最保守的产品模拟）
    sens["expand_top4"] = expand_metrics(base_order, FINAL_K, EXPAND_WINDOW)
    sens["mmr_expand_top4"] = expand_metrics(mmr_full, FINAL_K, EXPAND_WINDOW)
    # 2) 窗口 ±2（docs/02 §9 提过）
    sens["expand_window"] = {}
    sens["mmr_expand_window"] = {}
    for window in WINDOW_SWEEP:
        sens["expand_window"][str(window)] = expand_metrics(
            base_order, EXPAND_SEED_K, window
        )
        sens["mmr_expand_window"][str(window)] = expand_metrics(
            mmr_full, EXPAND_SEED_K, window
        )
    # 3) MMR λ 扫描
    sens["lambda_sweep"] = {}
    for sweep_lam in LAMBDA_SWEEP:
        order = mmr_full if abs(sweep_lam - lam) < 1e-12 else mmr_order(
            pool, pair_sim, sims, lam=sweep_lam, select_n=len(pool)
        )
        rank = first_gold_rank(order, gold)
        sens["lambda_sweep"][str(sweep_lam)] = {
            "hit1": hit_at(order, gold, 1),
            "hit4": hit_at(order, gold, 4),
            "hit10": hit_at(order, gold, 10),
            "hit20": hit_at(order, gold, 20),
            "mrr": (1.0 / rank) if rank else 0.0,
        }
    # 4) MMR 候选池 20（原型口径；对全部题都算，保证与池 50 同分母可比）
    pool20 = base_order[: min(20, len(base_order))]
    order20 = mmr_order(
        pool20,
        cosine_matrix(matrix[np.asarray(pool20)]),
        sims,
        lam=lam,
        select_n=len(pool20),
    )
    sens["mmr_pool20"] = {
        "hit1": hit_at(order20, gold, 1),
        "hit4": hit_at(order20, gold, 4),
        "hit10": hit_at(order20, gold, 10),
        "hit20": hit_at(order20, gold, 20),
    }
    record["sensitivity"] = sens
    return record


# =========================================================== 聚合 / bootstrap
def paired_bootstrap(diffs: np.ndarray, *, n: int = BOOTSTRAP_N, seed: int = SEED) -> dict:
    count = len(diffs)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, count, size=(n, count))
    means = diffs[idx].mean(axis=1)
    low, high = np.percentile(means, [2.5, 97.5])
    return {
        "delta": float(diffs.mean()),
        "ci95": [float(low), float(high)],
        "excludes_zero": bool(low > 0 or high < 0),
        "lower_gt_zero": bool(low > 0),
        "p_gt_zero": float((means > 0).mean()),
        "n_questions": count,
        "n_bootstrap": n,
    }


def metric_accessor(records: list[dict], config: str, field: str):
    return np.array([float(record[config][field]) for record in records], dtype=float)


def summarize_config(records: list[dict], config: str, fields: tuple[str, ...]) -> dict:
    out = {}
    for field in fields:
        values = metric_accessor(records, config, field)
        out[field] = float(values.mean()) if len(values) else 0.0
    return out


COMPARISONS: dict[str, tuple[str, str, str]] = {
    # 名字 -> (左 config/metric, 右 config/metric, 人类可读)
    "mmr - baseline @1": ("mmr", "hit1", "baseline", "hit1"),
    "mmr - baseline @4": ("mmr", "hit4", "baseline", "hit4"),
    "mmr - baseline @10": ("mmr", "hit10", "baseline", "hit10"),
    "mmr - baseline @20": ("mmr", "hit20", "baseline", "hit20"),
    "mmr - baseline mrr": ("mmr", "mrr", "baseline", "mrr"),
    "expand - baseline @4+expand": ("expand", "hit_set", "baseline", "hit4"),
    "mmr_expand - mmr @4+expand": ("mmr_expand", "hit_set", "mmr", "hit4"),
    "mmr_expand - baseline @4+expand": ("mmr_expand", "hit_set", "baseline", "hit4"),
}


def compare(records: list[dict], spec: tuple[str, str, str, str]) -> dict:
    left_config, left_field, right_config, right_field = spec
    return compare_values(
        metric_accessor(records, left_config, left_field),
        metric_accessor(records, right_config, right_field),
    )


def compare_values(left: np.ndarray, right: np.ndarray) -> dict:
    if not len(left):
        return {
            "delta": 0.0,
            "ci95": [0.0, 0.0],
            "excludes_zero": False,
            "lower_gt_zero": False,
            "p_gt_zero": 0.0,
            "n_questions": 0,
            "n_bootstrap": 0,
            "better": 0,
            "worse": 0,
            "tie": 0,
            "left_mean": 0.0,
            "right_mean": 0.0,
            "empty": True,
        }
    diffs = left - right
    stats = paired_bootstrap(diffs)
    stats["better"] = int((diffs > 0).sum())
    stats["worse"] = int((diffs < 0).sum())
    stats["tie"] = int((diffs == 0).sum())
    stats["left_mean"] = float(left.mean())
    stats["right_mean"] = float(right.mean())
    return stats


def group_summary(records: list[dict], label: str) -> dict:
    baseline_fields = ("hit1", "hit4", "hit10", "hit20", "cov4", "cov10", "cov20", "mrr")
    mmr_fields = baseline_fields
    result = {
        "label": label,
        "n_questions": len(records),
        "n_single_gold": int(sum(1 for r in records if r["n_gold"] == 1)),
        "n_multi_gold": int(sum(1 for r in records if r["n_gold"] > 1)),
        "baseline": summarize_config(records, "baseline", baseline_fields),
        "mmr": summarize_config(records, "mmr", mmr_fields),
        "expand": summarize_config(
            records, "expand", ("hit_set", "cov_set", "set_size", "n_neighbors", "recovered", "seed6_has_gold")
        ),
        "mmr_expand": summarize_config(
            records, "mmr_expand", ("hit_set", "cov_set", "set_size", "n_neighbors", "recovered", "seed6_has_gold")
        ),
        "comparisons": {
            name: compare(records, spec) for name, spec in COMPARISONS.items()
        },
    }
    # 敏感性（只报均值，不重复 bootstrap）
    sens = {
        "expand_top4_hit_set": float(
            np.mean([r["sensitivity"]["expand_top4"]["hit_set"] for r in records])
        ),
        "mmr_expand_top4_hit_set": float(
            np.mean([r["sensitivity"]["mmr_expand_top4"]["hit_set"] for r in records])
        ),
        "expand_window2_hit_set": float(
            np.mean([r["sensitivity"]["expand_window"]["2"]["hit_set"] for r in records])
        ),
        "mmr_expand_window2_hit_set": float(
            np.mean([r["sensitivity"]["mmr_expand_window"]["2"]["hit_set"] for r in records])
        ),
        "lambda_sweep": {},
        "mmr_pool20": {},
    }
    for lam_key in (str(x) for x in LAMBDA_SWEEP):
        values = [r["sensitivity"]["lambda_sweep"][lam_key] for r in records]
        sens["lambda_sweep"][lam_key] = {
            key: float(np.mean([v[key] for v in values]))
            for key in ("hit1", "hit4", "hit10", "hit20", "mrr")
        }
    pool_values = [r["sensitivity"].get("mmr_pool20") for r in records]
    pool_values = [v for v in pool_values if v]
    if pool_values:
        sens["mmr_pool20"] = {
            key: float(np.mean([v[key] for v in pool_values]))
            for key in ("hit1", "hit4", "hit10", "hit20")
        }
        sens["mmr_pool20"]["n_questions"] = len(pool_values)
    result["sensitivity"] = sens
    # 敏感性变体的配对 Δ + CI（与主表同口径，方便直接读"换参数后还成不成立"）
    base_hit4 = metric_accessor(records, "baseline", "hit4")
    variants = {
        "expand_top4 - baseline @4": np.array(
            [r["sensitivity"]["expand_top4"]["hit_set"] for r in records], dtype=float
        ),
        "expand_window2 - baseline @4": np.array(
            [r["sensitivity"]["expand_window"]["2"]["hit_set"] for r in records],
            dtype=float,
        ),
        "mmr_expand_top4 - baseline @4": np.array(
            [r["sensitivity"]["mmr_expand_top4"]["hit_set"] for r in records],
            dtype=float,
        ),
        "mmr_pool20 - baseline @4": np.array(
            [r["sensitivity"]["mmr_pool20"]["hit4"] for r in records], dtype=float
        ),
    }
    for lam_key in ("0.5", "0.6", "0.8", "0.9"):
        variants[f"lambda{lam_key} - baseline @4"] = np.array(
            [r["sensitivity"]["lambda_sweep"][lam_key]["hit4"] for r in records],
            dtype=float,
        )
    result["sensitivity_comparisons"] = {
        name: compare_values(values, base_hit4) for name, values in variants.items()
    }
    return result


def shuffle_control(records: list[dict], seed: int = SEED) -> dict:
    """零假设对照：逐题随机交换两配置的取值（H0：两者等价）。

    用途：量出"没有真实差异时"Δ 的波动幅度（|Δ| p95），确认 bootstrap CI 不是
    靠样本量硬挤出来的假阳性（不花调用）。
    """
    rng = np.random.default_rng(seed)
    base4 = metric_accessor(records, "baseline", "hit4")
    mmr4 = metric_accessor(records, "mmr", "hit4")
    # 逐题配对置换（H0：两配置等价）→ 置换检验的 Δ 分布
    perms = np.empty(500)
    for i in range(500):
        flip = rng.random(len(base4)) < 0.5
        perm = np.where(flip, mmr4, base4)
        other = np.where(flip, base4, mmr4)
        perms[i] = (perm - other).mean()
    observed = float((mmr4 - base4).mean())
    return {
        "observed_delta_mmr_r4": observed,
        "perm_delta_p95": float(np.percentile(np.abs(perms), 95)),
        "observed_abs_gt_p95": bool(abs(observed) > np.percentile(np.abs(perms), 95)),
    }


# =========================================================== 主流程
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--smoke", action="store_true", help="只跑前 3 题（冒烟）")
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 题")
    parser.add_argument("--offline", action="store_true", help="只读缓存，0 调用")
    parser.add_argument("--workers", type=int, default=EMBED_WORKERS)
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    parser.add_argument("--out", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    global BATCH_SIZE, EMBED_WORKERS
    args = build_parser().parse_args(argv)
    BATCH_SIZE = max(1, int(args.batch_size))
    EMBED_WORKERS = max(1, int(args.workers))

    limit = 3 if args.smoke else args.limit
    out_path = args.out or (SMOKE_OUT_PATH if args.smoke else OUT_PATH)

    rows = load_dataset(DATA_PATH)
    if limit:
        rows = rows[:limit]
    print(f"题目：{len(rows)}（数据集 {DATA_PATH.name}）")

    _load_dotenv(str(REPO_ROOT / ".env"))
    cfg = load_config()
    provider = build_provider(cfg)
    install_usage_capture()
    store = EmbeddingStore(CACHE_PATH, provider, offline=args.offline)
    print(
        f"provider：{cfg.embedding_backend} / {cfg.embedding_model} / "
        f"dim={cfg.embedding_dim}"
    )

    # 一次性把要用的文本都算出来（facts 用产品索引文本，问题用 normalize 后的查询）
    texts: list[str] = []
    fact_texts_by_q: list[list[str]] = []
    queries: list[str] = []
    for row in rows:
        fact_texts = [fact_index_text(fact) for fact in row["facts"]]
        query = question_text(row)
        fact_texts_by_q.append(fact_texts)
        queries.append(query)
        texts.extend(fact_texts)
        texts.append(query)
    started = time.perf_counter()
    store.ensure(texts)
    embed_seconds = time.perf_counter() - started

    records: list[dict] = []
    for index, (row, fact_texts, query) in enumerate(
        zip(rows, fact_texts_by_q, queries), 1
    ):
        record = evaluate_question(row, fact_texts, query, store)
        records.append(record)
        if index % 50 == 0 or index == len(rows):
            print(f"  评估 {index}/{len(rows)}")

    groups = {
        "all": group_summary(records, "全部 337 题"),
        "single_gold": group_summary(
            [r for r in records if r["n_gold"] == 1], "单 gold 题"
        ),
        "multi_gold": group_summary(
            [r for r in records if r["n_gold"] > 1], "多 gold 题"
        ),
        "answerable": group_summary(
            [r for r in records if not r["abstention"]], "非 abstention 子集"
        ),
        "abstention": group_summary(
            [r for r in records if r["abstention"]], "abstention 子集"
        ),
    }

    # 诊断：基线漏在哪、展开能救多少
    all_records = records
    miss50 = [r for r in all_records if r["gold_rank"] is None]
    miss4 = [r for r in all_records if not r["baseline"]["hit4"]]
    recovery = {
        "n_questions": len(all_records),
        "n_gold_missed_top50": len(miss50),
        "n_gold_missed_top4": len(miss4),
        "expand_recovered": int(sum(r["expand"]["recovered"] for r in all_records)),
        "mmr_expand_recovered": int(
            sum(r["mmr_expand"]["recovered"] for r in all_records)
        ),
        "expand_set_size_mean": float(
            np.mean([r["expand"]["set_size"] for r in all_records])
        ),
        "expand_set_size_max": int(max(r["expand"]["set_size"] for r in all_records)),
        "expand_seed6_has_gold": int(
            sum(r["expand"]["seed6_has_gold"] for r in all_records)
        ),
        "expand_neighbors_mean": float(
            np.mean([r["expand"]["n_neighbors"] for r in all_records])
        ),
        "current_impl_fires": int(
            sum(1 for r in all_records if r["n_facts"] < FINAL_K)
        ),
    }

    # 决策判据：Δ 的 95% CI 下界 > 0
    def decision_entry(comparison: dict, metric: str) -> dict:
        return {
            "enable": bool(comparison["ci95"][0] > 0),
            "metric": metric,
            "delta": comparison["delta"],
            "ci95": comparison["ci95"],
            "excludes_zero": comparison["excludes_zero"],
            "better": comparison["better"],
            "worse": comparison["worse"],
            "tie": comparison["tie"],
            "left_mean": comparison["left_mean"],
            "right_mean": comparison["right_mean"],
            "n_questions": comparison["n_questions"],
        }

    def gate_groups(group_name: str) -> dict:
        comparisons = groups[group_name]["comparisons"]
        return {
            "mmr": decision_entry(comparisons["mmr - baseline @4"], "hit@4"),
            "expand": decision_entry(comparisons["expand - baseline @4+expand"],
                                     "evidence-in-set@4+expand"),
            "mmr_expand": decision_entry(
                comparisons["mmr_expand - baseline @4+expand"],
                "evidence-in-set@4+expand",
            ),
        }

    all_comparisons = groups["all"]["comparisons"]
    decision = {
        "criterion": "Δ 的 95% CI 下界 > 0",
        "mmr": decision_entry(all_comparisons["mmr - baseline @4"], "hit@4"),
        "expand": decision_entry(
            all_comparisons["expand - baseline @4+expand"],
            "evidence-in-set@4+expand",
        ),
        "mmr_expand": decision_entry(
            all_comparisons["mmr_expand - baseline @4+expand"],
            "evidence-in-set@4+expand",
        ),
        "groups": {
            name: gate_groups(name)
            for name in ("single_gold", "multi_gold", "answerable", "abstention")
        },
    }

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset": {
            "path": str(DATA_PATH.relative_to(REPO_ROOT)).replace("\\", "/"),
            "questions": len(rows),
            "facts": int(sum(len(r["facts"]) for r in rows)),
            "sessions": len({f["session_id"] for r in rows for f in r["facts"]}),
            "single_gold": groups["all"]["n_single_gold"],
            "multi_gold": groups["all"]["n_multi_gold"],
            "abstention": int(sum(1 for r in rows if r.get("is_abstention"))),
        },
        "config": {
            "route_top_k": ROUTE_TOP_K,
            "final_k": FINAL_K,
            "mmr_lambda": MMR_LAMBDA,
            "mmr_pool": MMR_POOL,
            "expand_seed_k": EXPAND_SEED_K,
            "expand_window": EXPAND_WINDOW,
            "ks": list(KS),
            "bootstrap_n": BOOTSTRAP_N,
            "seed": SEED,
            "fact_text": "index_text_for（v2：日期前缀 + 正文）",
            "query_text": "textnorm.normalize(question)",
            "metric_primary": "hit@k（任一 gold 证据在 top-k）",
            "metric_secondary": "cov@k（gold 覆盖比例）",
            "expand_metric": "evidence-in-set@4+expand（top-4 ∪ 同 session ±1 邻居）",
        },
        "embeddings": {
            "model": cfg.embedding_model,
            "backend": cfg.embedding_backend,
            "dim": store.dim,
            "texts_total": int(len(texts)),
            "texts_unique": int(len(set(texts))),
            "cache_hits_this_run": store.cache_hits,
            "texts_embedded_this_run": store.new_texts,
            "api_calls_this_run": store.calls,
            "prompt_tokens_this_run": store.tokens,
            # 缓存文件里记着**建缓存累计**的真实花费（离线复跑不清零）
            "cache_build": {
                "api_calls": store.usage.get("api_calls"),
                "texts": store.usage.get("texts"),
                "prompt_tokens": store.usage.get("prompt_tokens"),
                "latency_ms_median": (
                    float(np.median(store.usage["latencies_ms"]))
                    if store.usage.get("latencies_ms")
                    else None
                ),
                "latency_ms_p95": (
                    float(np.percentile(store.usage["latencies_ms"], 95))
                    if store.usage.get("latencies_ms")
                    else None
                ),
            },
            "cache_file": CACHE_PATH.name,
            "cache_size_mb": (
                round(CACHE_PATH.stat().st_size / 1e6, 1) if CACHE_PATH.exists() else None
            ),
            "wall_seconds_this_run": round(embed_seconds, 1),
            "price_note": "阿里云 MaaS 未确认单价，只报实测 token（不编价格）",
        },
        "runs": {name: groups["all"][name] for name in ("baseline", "mmr", "expand", "mmr_expand")},
        "groups": groups,
        "diagnostics": recovery,
        "shuffle_control": shuffle_control(records),
        "decision": decision,
        "per_query": records,
    }

    out_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n结果写入 {out_path}")

    # 终端摘要
    print("\n== 主结果（全部 {} 题，hit@k 主口径） ==".format(len(records)))
    for name, run in groups["all"].items():
        if name == "label":
            continue
        if name in ("baseline", "mmr"):
            print(
                f"{name:10s} R@1 {run['hit1']:.4f}  R@4 {run['hit4']:.4f}  "
                f"R@10 {run['hit10']:.4f}  R@20 {run['hit20']:.4f}  MRR {run['mrr']:.4f}"
            )
        elif name in ("expand", "mmr_expand"):
            print(
                f"{name:10s} evidence-in-set@4+expand {run['hit_set']:.4f}  "
                f"recovered {run['recovered']:+.4f}  set_size {run['set_size']:.2f}"
            )
    print("\n== 配对 Δ（2000 次 bootstrap，95% CI） ==")
    for name, stats in groups["all"]["comparisons"].items():
        print(
            f"{name:34s} Δ {stats['delta']:+.4f}  CI [{stats['ci95'][0]:+.4f}, "
            f"{stats['ci95'][1]:+.4f}]  {stats['better']}/{stats['worse']}/{stats['tie']}"
        )
    print("\n== 决策（CI 下界 > 0 才启用） ==")
    for key in ("mmr", "expand", "mmr_expand"):
        entry = decision[key]
        ci = entry["ci95"]
        print(
            f"{key:12s} {'启用' if entry['enable'] else '不启用'}  "
            f"Δ {entry['delta']:+.4f}  CI [{ci[0]:+.4f}, {ci[1]:+.4f}]  "
            f"{entry['better']}/{entry['worse']}/{entry['tie']}"
        )
    print(
        "分组：single_gold mmr Δ "
        f"{decision['groups']['single_gold']['mmr']['delta']:+.4f} / "
        f"multi_gold {decision['groups']['multi_gold']['mmr']['delta']:+.4f}"
    )
    print(
        "      单 gold 展开 Δ "
        f"{decision['groups']['single_gold']['expand']['delta']:+.4f} / "
        f"多 gold {decision['groups']['multi_gold']['expand']['delta']:+.4f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
