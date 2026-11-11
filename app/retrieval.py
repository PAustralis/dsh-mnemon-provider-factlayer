"""检索层（第二层）：六路召回 + RRF 融合 + 裁剪（作废过滤 / R5 纠正后补 / MMR / 会话展开）。

路线用**语义名**（vector / bigram / trigram / graph / recent / correction），
配置与日志都不使用 R1..R6 编号（编号在 REPORT 与设计文档间错位过）。

- 每路独立开关（``RETRIEVAL_ROUTES``），关掉任一路不丢数据、不报错。
- 每路 SQL 内部用共享 CTE 排除作废记录（per-pair 最新边语义，见 ``app.voided``）。
- 向量路故障**降级**：超时/报错跳过 vector，其余路照常，计数并进 /health；
  ``model_id + dim`` 不符是配置事故 → 抛 :class:`EmbeddingModelMismatch`，
  API 层拒绝检索（409）并提示 ``POST /admin/rebuild``，不自动重建。
- R5 纠正后补：只对被作废而剔除的候选生效（任一路「未过滤排名 ≤ FINAL_K」视为
  「会进入前 6」的可判定近似），沿 supersedes 边拉纠正者、最多 3 跳、继承名次，
  不参与 RRF 打分、不占常规名额；多纠正者并存全部保留。
- R6 最近 50：仅当前几路去重后 < FINAL_K 时兜底。
- 会话邻域展开：融合与 MMR 之后，同 session_id ±2 条，不占名额、继承父项名次、
  不重新打分、必须过作废过滤；重叠邻域按「父项名次 → recorded_at → id」确定性合并。
- 输出上限：``EXPAND_MAX_ITEMS``（含本体）/ ``EXPAND_MAX_CHARS``，超出截断并标注。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field

import numpy as np

from . import textnorm
from . import voided as voided_mod
from .config import Config
from .db import Database
from .embeddings import (
    EmbeddingModelMismatch,
    EmbeddingProvider,
    VectorIndex,
    check_fingerprint,
    normalize_vector,
)
from .read_path import TRUNCATE_LIMIT, truncate_for_output

logger = logging.getLogger("factlayer.retrieval")

MMR_POOL = 20
EVAL_META_KEY = "retrieval:eval"
RECENT_FALLBACK_LIMIT = 50
EXPAND_WINDOW = 2


@dataclass
class RouteOutcome:
    """单路一次查询的结果（raw_ids = 去重/截断前，ids = 去重截断后）。"""

    name: str
    ids: list[str] = field(default_factory=list)
    raw_ids: list[str] = field(default_factory=list)
    near_misses: list[tuple[str, int]] = field(default_factory=list)
    error: str | None = None
    skipped: str | None = None
    unavailable: bool = False
    fallback: bool = False
    deduped_count: int = 0
    truncated_count: int = 0

    @property
    def raw_count(self) -> int:
        return len(self.raw_ids) if self.raw_ids else len(self.ids)


class RetrievalStats:
    """每路最近空结果率 + 事件计数（/health 暴露，进程内滚动窗口）。"""

    ROUTES = ("vector", "bigram", "trigram", "graph", "recent")

    def __init__(self, window: int = 200) -> None:
        self._lock = threading.Lock()
        self._window = window
        self.route_queries = {name: 0 for name in self.ROUTES}
        self.route_empty = {name: 0 for name in self.ROUTES}
        self.route_errors = {name: 0 for name in self.ROUTES}
        self.route_unavailable = {name: 0 for name in self.ROUTES}
        self._recent_empty = {name: deque(maxlen=window) for name in self.ROUTES}
        self.queries = 0
        self.phrase_fallback_count = 0
        self.vector_degraded_count = 0
        self.last_vector_error: str | None = None

    def record_query(self, outcomes: list[RouteOutcome]) -> None:
        with self._lock:
            self.queries += 1
            for outcome in outcomes:
                if outcome.name not in self.route_queries:
                    continue
                self.route_queries[outcome.name] += 1
                empty = 1 if not outcome.ids else 0
                self.route_empty[outcome.name] += empty
                self._recent_empty[outcome.name].append(empty)
                if outcome.error:
                    self.route_errors[outcome.name] += 1
                if outcome.unavailable:
                    self.route_unavailable[outcome.name] += 1

    def record_phrase_fallback(self) -> None:
        with self._lock:
            self.phrase_fallback_count += 1

    def record_vector_degraded(self, message: str) -> None:
        with self._lock:
            self.vector_degraded_count += 1
            self.last_vector_error = message

    def snapshot(self) -> dict:
        with self._lock:
            routes = {}
            for name in self.ROUTES:
                recent = self._recent_empty[name]
                routes[name] = {
                    "queries": self.route_queries[name],
                    "empty": self.route_empty[name],
                    "empty_rate": (sum(recent) / len(recent)) if recent else None,
                    "errors": self.route_errors[name],
                    "unavailable": self.route_unavailable[name],
                }
            return {
                "queries": self.queries,
                "routes": routes,
                "phrase_fallback_count": self.phrase_fallback_count,
                "vector_degraded_count": self.vector_degraded_count,
                "last_vector_error": self.last_vector_error,
            }


def read_eval_snapshot(db: Database) -> dict | None:
    """读基线脚本写入的评测快照（meta: retrieval:eval）；没有则 None。"""
    row = db.read_conn.execute(
        "SELECT value FROM meta WHERE key=?", (EVAL_META_KEY,)
    ).fetchone()
    if row is None:
        return None
    try:
        data = json.loads(row["value"])
    except (TypeError, ValueError):
        logger.warning("meta %s 不是合法 JSON，忽略", EVAL_META_KEY)
        return None
    return data if isinstance(data, dict) else None


def resolve_routes(db: Database, cfg: Config) -> tuple[tuple[str, ...], dict]:
    """生效路线 + 自动降级状态。

    验收 §6.3：融合结果低于最好单路时自动收窄到最好单路（默认即 bigram），
    不要求 owner 手工改配置、降级本身落日志。运行时没有相关性标签，因此用
    「基线 harness 写入 meta:retrieval:eval 的评测快照」作为唯一可判定依据
    （无快照 = 不降级）。
    """
    routes = cfg.route_names
    degrade = {"degraded": False, "reason": None}
    if not cfg.auto_degrade:
        return routes, degrade
    snapshot = read_eval_snapshot(db)
    if not snapshot:
        return routes, degrade
    best = snapshot.get("best_single_route")
    try:
        fusion = float(snapshot.get("fusion_r6"))
        best_r6 = float(snapshot.get("best_single_r6"))
    except (TypeError, ValueError):
        return routes, degrade
    if best in routes and fusion < best_r6:
        degrade = {
            "degraded": True,
            "reason": (
                f"融合 R@6={fusion:.4f} 低于最好单路 {best} R@6={best_r6:.4f}；"
                f"路线自动收窄为 {best}"
            ),
        }
        logger.warning(
            "retrieval auto-degrade: %s（快照来自 meta:%s）",
            degrade["reason"],
            EVAL_META_KEY,
        )
        return (best,), degrade
    return routes, degrade


class RetrievalService:
    def __init__(
        self,
        db: Database,
        cfg: Config,
        embedding_provider: EmbeddingProvider | None = None,
        *,
        index: VectorIndex | None = None,
        stats: RetrievalStats | None = None,
    ) -> None:
        self.db = db
        self.cfg = cfg
        self.embedding_provider = embedding_provider
        self.index = index or VectorIndex()
        self.stats = stats or RetrievalStats()
        self.routes, self.degrade = resolve_routes(db, cfg)

    def invalidate_caches(self) -> None:
        self.index.invalidate()

    # ------------------------------------------------------------------ 单路
    def _fts_rank(
        self, table: str, expr: str | None, limit: int, *, filtered: bool
    ) -> list[str]:
        if not expr:
            return []
        condition = voided_mod.NOT_VOIDED_SQL if filtered else "1=1"
        sql = (
            voided_mod.VOIDED_CTE
            + f" SELECT m.id FROM {table}"
            f" JOIN memories m ON m.rowid = {table}.rowid"
            f" WHERE {table} MATCH ? AND {condition}"
            f" ORDER BY bm25({table}) ASC, m.id ASC LIMIT ?"
        )
        rows = self.db.read_conn.execute(sql, (expr, int(limit))).fetchall()
        return [row[0] for row in rows]

    def _recent_rank(self, limit: int, *, filtered: bool) -> list[str]:
        condition = voided_mod.NOT_VOIDED_SQL if filtered else "1=1"
        sql = (
            voided_mod.VOIDED_CTE
            + " SELECT m.id FROM memories m"
            f" WHERE {condition}"
            " ORDER BY m.recorded_at DESC, m.id DESC LIMIT ?"
        )
        rows = self.db.read_conn.execute(sql, (int(limit),)).fetchall()
        return [row[0] for row in rows]

    @staticmethod
    def _near_misses(
        unfiltered: list[str], filtered: list[str]
    ) -> list[tuple[str, int]]:
        kept = set(filtered)
        return [
            (mid, rank) for rank, mid in enumerate(unfiltered, 1) if mid not in kept
        ]

    def bigram_route(self, query: str, limit: int) -> RouteOutcome:
        """R2 二元组：双字短语优先 → 0 命中回退单字（计数 + 日志）→ <2 字直接单字。"""
        norm = textnorm.normalize(query)
        direct_single = len(norm) < 2
        fallback = False
        if direct_single:
            # 查询串 < 2 字：直接走单字，不算「短语→OR 回退」
            expr = textnorm.fts_match_expr(textnorm.unigram_query_tokens(norm))
        else:
            expr = textnorm.fts_match_expr(textnorm.bigram_query_tokens(norm))
        unfiltered = self._fts_rank("memories_fts_bigram", expr, limit, filtered=False)
        if expr and not unfiltered and not direct_single:
            # 短语 0 命中 → 回退 OR 单字互连；必须计数 + 打日志（不许静默 0 命中）
            expr = textnorm.fts_match_expr(textnorm.unigram_query_tokens(norm))
            self.stats.record_phrase_fallback()
            fallback = True
            logger.warning(
                "bigram 短语 0 命中，回退 OR 单字：query=%r（累计回退 %d 次）",
                query,
                self.stats.phrase_fallback_count,
            )
            unfiltered = self._fts_rank(
                "memories_fts_bigram", expr, limit, filtered=False
            )
        filtered = self._fts_rank("memories_fts_bigram", expr, limit, filtered=True)
        return RouteOutcome(
            name="bigram",
            ids=filtered,
            raw_ids=filtered,
            near_misses=self._near_misses(unfiltered, filtered),
            fallback=fallback,
        )

    def trigram_route(self, query: str, limit: int) -> RouteOutcome:
        """R3 trigram：独立 FTS5 虚表；1–2 字查询在此路不可用（明确跳过并计数）。"""
        expr = textnorm.fts_match_expr(textnorm.trigram_tokens(query))
        if not expr:
            return RouteOutcome(name="trigram", skipped="query_shorter_than_3_chars")
        unfiltered = self._fts_rank("memories_fts_trigram", expr, limit, filtered=False)
        filtered = self._fts_rank("memories_fts_trigram", expr, limit, filtered=True)
        return RouteOutcome(
            name="trigram",
            ids=filtered,
            raw_ids=filtered,
            near_misses=self._near_misses(unfiltered, filtered),
        )

    def graph_route(self, query: str, limit: int) -> RouteOutcome:
        """R4 图邻居：只留接口与开关，默认关；未实现时明确返回空并计数。"""
        logger.warning(
            "graph 路线已启用但本层未实现（FalkorDB 实体 1 跳邻居）；"
            "本次返回空结果并计入 /health.retrieval.routes.graph.unavailable"
        )
        return RouteOutcome(
            name="graph", ids=[], unavailable=True, skipped="graph_not_implemented"
        )

    def recent_route(self, query: str, limit: int) -> RouteOutcome:
        unfiltered = self._recent_rank(limit, filtered=False)
        filtered = self._recent_rank(limit, filtered=True)
        return RouteOutcome(
            name="recent",
            ids=filtered,
            raw_ids=filtered,
            near_misses=self._near_misses(unfiltered, filtered),
        )

    def vector_route(self, query: str, limit: int) -> RouteOutcome:
        """R1 向量：内存矩阵暴力点积（只回 id + 距离）。

        ``EmbeddingModelMismatch`` 向上抛（配置事故 → 409）；其他异常由 search()
        捕获并降级（跳过本路、其余路照常、计数进 /health）。
        """
        if self.embedding_provider is None:
            return RouteOutcome(
                name="vector", unavailable=True, skipped="embedding_provider_missing"
            )
        check_fingerprint(self.db, self.embedding_provider, self.cfg.embedding_dim_int)
        ids, matrix = self.index.load(self.db)
        if not ids or matrix.size == 0:
            return RouteOutcome(name="vector", skipped="empty_embedding_index")
        query_vectors = self.embedding_provider.embed([textnorm.normalize(query)])
        if not query_vectors:
            raise RuntimeError("embedding provider 返回空结果")
        query_vector = normalize_vector(query_vectors[0])
        if query_vector.shape[0] != matrix.shape[1]:
            raise EmbeddingModelMismatch(
                f"查询向量维度 {query_vector.shape[0]} 与库内矩阵维度 "
                f"{matrix.shape[1]} 不符；请 POST /admin/rebuild"
            )
        sims = matrix @ query_vector
        order = np.argsort(-sims, kind="stable")
        voided = voided_mod.voided_id_set(self.db.read_conn)
        unfiltered = [ids[i] for i in order[: int(limit)]]
        filtered = [ids[i] for i in order if ids[i] not in voided][: int(limit)]
        return RouteOutcome(
            name="vector",
            ids=filtered,
            raw_ids=filtered,
            near_misses=self._near_misses(unfiltered, filtered),
        )

    # ------------------------------------------------------------------ 主流程
    def search(
        self,
        query: str,
        *,
        session_id: str | None = None,
        limit: int | None = None,
    ) -> dict:
        started = time.perf_counter()
        timings: dict[str, float] = {}
        norm_query = textnorm.normalize(query or "")
        if not norm_query:
            raise ValueError("query 不能为空")
        final_k = int(self.cfg.final_k)
        if limit is not None:
            final_k = max(1, min(final_k, int(limit)))
        route_limit = int(self.cfg.route_top_k)

        outcomes: list[RouteOutcome] = []
        for name in self.routes:
            route_started = time.perf_counter()
            try:
                if name == "vector":
                    outcome = self.vector_route(norm_query, route_limit)
                elif name == "bigram":
                    outcome = self.bigram_route(norm_query, route_limit)
                elif name == "trigram":
                    outcome = self.trigram_route(norm_query, route_limit)
                elif name == "graph":
                    outcome = self.graph_route(norm_query, route_limit)
                else:  # pragma: no cover - 配置校验已拦截
                    raise ValueError(f"unknown route: {name}")
            except EmbeddingModelMismatch:
                raise
            except Exception as exc:  # 向量路故障降级：不 500、不静默空结果
                logger.exception("%s 路线失败，本次跳过（其余路照常）", name)
                self.stats.record_vector_degraded(
                    f"{name}: {type(exc).__name__}: {exc}"
                )
                outcome = RouteOutcome(
                    name=name, ids=[], error=f"{type(exc).__name__}: {exc}"
                )
            finally:
                timings[f"route_{name}"] = round(
                    (time.perf_counter() - route_started) * 1000, 3
                )
            outcomes.append(outcome)

        # 每路先去重再截断（同一 id 在一路内重复不重复计分）
        for outcome in outcomes:
            deduped = _dedupe(outcome.ids)
            outcome.deduped_count = len(deduped)
            outcome.truncated_count = max(0, len(deduped) - route_limit)
            outcome.ids = deduped[:route_limit]

        distinct_before_fallback = {mid for outcome in outcomes for mid in outcome.ids}
        recent_fallback = False
        if len(distinct_before_fallback) < final_k:
            recent_started = time.perf_counter()
            recent = self.recent_route(norm_query, RECENT_FALLBACK_LIMIT)
            recent.deduped_count = len(_dedupe(recent.ids))
            recent.truncated_count = max(0, recent.deduped_count - RECENT_FALLBACK_LIMIT)
            recent.ids = _dedupe(recent.ids)[:RECENT_FALLBACK_LIMIT]
            timings["route_recent"] = round(
                (time.perf_counter() - recent_started) * 1000, 3
            )
            outcomes.append(recent)
            recent_fallback = True
            logger.info(
                "R6 最近 50 兜底触发：去重后 %d 条 < FINAL_K=%d",
                len(distinct_before_fallback),
                final_k,
            )

        # ---- RRF：score(d) = Σ 1/(k + rank_i) --------------------------------
        fuse_started = time.perf_counter()
        route_lists = [(o.name, o.ids) for o in outcomes if o.ids]
        scores, _ranks = _rrf_scores(route_lists, int(self.cfg.rrf_k))
        candidate_ids = list(scores)
        details = self._load_details(candidate_ids)
        order = _rank_candidates(scores, details)
        fused_count = len(order)
        timings["fusion"] = round((time.perf_counter() - fuse_started) * 1000, 3)

        # ---- MMR（默认关；开启时在融合池上迭代选样） --------------------------
        mmr_degraded = False
        if self.cfg.mmr_enabled and order:
            order, mmr_degraded = self._apply_mmr(order, scores, final_k)
        selected = order[:final_k]

        # ---- 作废剔除（防御性闸门） + R5 纠正后补 ----------------------------
        voided_set = voided_mod.voided_id_set(self.db.read_conn)
        dropped_scored = [mid for mid in selected if mid in voided_set]
        selected = [mid for mid in selected if mid not in voided_set]
        correction = self._correction_supplement(
            outcomes, dropped_scored, selected, voided_set, final_k=final_k
        )
        scored_selected = list(selected)
        all_ids = scored_selected + correction["ids"]

        # ---- 会话邻域展开 ----------------------------------------------------
        expand_started = time.perf_counter()
        expansion = self._expand(
            scored_selected,
            details,
            voided_set,
            max_items=int(self.cfg.expand_max_items),
            max_chars=int(self.cfg.expand_max_chars),
            already=all_ids,
        )
        timings["expansion"] = round((time.perf_counter() - expand_started) * 1000, 3)

        # ---- 组装输出（截断自报 + 总量上限） ---------------------------------
        output_ids = all_ids + expansion["ids"]
        details = {**details, **expansion["details"]}
        chars = 0
        items: list[dict] = []
        truncated_items = 0
        for position, mid in enumerate(output_ids):
            detail = details.get(mid)
            if detail is None:
                continue
            raw_content = detail["content"]
            content = truncate_for_output(raw_content, mid)
            if chars + len(content) > int(self.cfg.expand_max_chars):
                truncated_items += len(output_ids) - position
                break
            chars += len(content)
            item = {
                "id": mid,
                "content": content,
                "content_truncated": len(raw_content) > TRUNCATE_LIMIT,
                "recorded_at": detail["recorded_at"],
                "session_id": detail["session_id"],
                "seq": detail["seq"],
                "score": round(scores[mid], 9) if mid in scores else None,
                "routes": _sources_for(mid, route_lists),
                "expanded": mid in expansion["parents"],
                "expanded_from": expansion["parents"].get(mid),
                "inherited_rank": None,
            }
            if mid in correction["meta"]:
                item["routes"] = ["correction"]
                item["score"] = None  # 纠正者不参与 RRF 打分（§3.5）
                item["inherited_rank"] = correction["meta"][mid]["inherited_rank"]
                item["inherited_from"] = correction["meta"][mid]["inherited_from"]
            items.append(item)
        if len(items) > int(self.cfg.expand_max_items):
            truncated_items += len(items) - int(self.cfg.expand_max_items)
            items = items[: int(self.cfg.expand_max_items)]

        timings["total"] = round((time.perf_counter() - started) * 1000, 3)
        self.stats.record_query(outcomes)

        phrase_fallback = any(outcome.fallback for outcome in outcomes)
        record = {
            "event": "search",
            "query_len": len(norm_query),
            "routes_enabled": list(self.routes),
            "route_raw": {o.name: o.raw_count for o in outcomes},
            "route_dedup": {o.name: o.deduped_count for o in outcomes},
            "route_truncated": {o.name: o.truncated_count for o in outcomes},
            "route_empty": {o.name: not o.ids for o in outcomes},
            "route_errors": {o.name: o.error for o in outcomes if o.error},
            "recent_fallback": recent_fallback,
            "phrase_to_or_fallback": phrase_fallback,
            "fused": fused_count,
            "selected": len(scored_selected),
            "voided_dropped": len(dropped_scored),
            "correction_candidates": correction["candidates"],
            "correction_supplemented": len(correction["ids"]),
            "correction_chain_dropped": correction["chain_dropped"],
            "expanded": len(expansion["ids"]),
            "expansion_truncated": expansion["truncated"],
            "final": len(items),
            "mmr": bool(self.cfg.mmr_enabled),
            "mmr_degraded": mmr_degraded,
            "degraded": self.degrade["degraded"],
            "elapsed_ms": timings,
        }
        logger.info("search %s", json.dumps(record, ensure_ascii=False))

        return {
            "query": query,
            "session_id": session_id,
            "count": len(items),
            "items": items,
            "limit": final_k,
            "routes": {
                o.name: {
                    "raw": o.raw_count,
                    "skipped": o.skipped,
                    "error": o.error,
                    "unavailable": o.unavailable,
                }
                for o in outcomes
            },
            "fallback": {"recent": recent_fallback, "phrase_to_or": phrase_fallback},
            "correction": {
                "candidates": correction["candidates"],
                "supplemented": len(correction["ids"]),
                "chain_dropped": correction["chain_dropped"],
            },
            "expansion": {
                "items": len(expansion["ids"]),
                "truncated": expansion["truncated"],
            },
            "output": {
                "max_items": int(self.cfg.expand_max_items),
                "max_chars": int(self.cfg.expand_max_chars),
                "truncated": bool(truncated_items),
                "truncated_items": truncated_items,
                "chars": chars,
            },
            "degraded": self.degrade,
            "elapsed_ms": timings,
        }

    # ------------------------------------------------------------------ 内部
    def _load_details(self, ids: list[str]) -> dict[str, dict]:
        if not ids:
            return {}
        unique = list(dict.fromkeys(ids))
        placeholders = ",".join("?" * len(unique))
        rows = self.db.read_conn.execute(
            "SELECT id, content, recorded_at, session_id, seq FROM memories "
            f"WHERE id IN ({placeholders})",
            unique,
        ).fetchall()
        return {
            row["id"]: {
                "id": row["id"],
                "content": row["content"],
                "recorded_at": row["recorded_at"],
                "session_id": row["session_id"],
                "seq": row["seq"],
            }
            for row in rows
        }

    def _apply_mmr(
        self, order: list[str], scores: dict[str, float], final_k: int
    ) -> tuple[list[str], bool]:
        """MMR：RRF 分先 min-max 归一化，用同一份归一化 embedding 的余弦做多样性。"""
        lm = float(self.cfg.mmr_lambda)
        pool = order[:MMR_POOL]
        if not pool:
            return order, False
        values = [scores[mid] for mid in pool]
        lo, hi = min(values), max(values)
        relevance = {
            mid: ((scores[mid] - lo) / (hi - lo) if hi > lo else 1.0 / (idx + 1))
            for idx, mid in enumerate(pool)
        }
        vectors: dict[str, np.ndarray] = {}
        try:
            ids, matrix = self.index.load(self.db)
            position = {mid: i for i, mid in enumerate(ids)}
            for mid in pool:
                idx = position.get(mid)
                if idx is not None:
                    vectors[mid] = matrix[idx]
        except Exception:  # 向量不可用：MMR 退化为 λ*相关度（仍确定性），并标记
            logger.exception("MMR 载入向量失败，退化处理")
        degraded = len(vectors) < len(pool)
        selected: list[str] = []
        rest = list(pool)
        while rest and len(selected) < min(len(pool), final_k):
            best: str | None = None
            best_value: float | None = None
            for mid in rest:
                divergence = 0.0
                if vectors and mid in vectors:
                    similarities = [
                        float(np.dot(vectors[mid], vectors[other]))
                        for other in selected
                        if other in vectors
                    ]
                    divergence = max(similarities, default=0.0)
                value = lm * relevance[mid] - (1.0 - lm) * divergence
                if best_value is None or value > best_value + 1e-12:
                    best, best_value = mid, value
            if best is None:  # pragma: no cover - rest 非空时必选出
                break
            selected.append(best)
            rest.remove(best)
        return selected, degraded

    def _correction_supplement(
        self,
        outcomes: list[RouteOutcome],
        dropped_scored: list[str],
        selected: list[str],
        voided_set: set[str],
        *,
        final_k: int,
    ) -> dict:
        """R5：对被作废而剔除的候选，沿 supersedes 拉纠正者继承名次。

        可判定近似：某候选在任一路的「未过滤排名」≤ FINAL_K，视为「本会进入前 6」。
        """
        best_rank: dict[str, int] = {}
        for outcome in outcomes:
            for mid, rank in outcome.near_misses:
                best_rank[mid] = min(best_rank.get(mid, 10**9), rank)
        for mid in dropped_scored:
            best_rank.setdefault(mid, final_k)
        eligible = sorted(
            ((rank, mid) for mid, rank in best_rank.items() if rank <= final_k),
            key=lambda item: (item[0], item[1]),
        )
        already = set(selected)
        added: list[str] = []
        meta: dict[str, dict] = {}
        chain_dropped = 0
        for rank, origin in eligible:
            if origin in already:
                continue
            correctors, dropped = voided_mod.correction_chain(
                self.db.read_conn, origin, max_hops=3, voided=voided_set
            )
            chain_dropped += dropped
            for corrector in correctors:
                if corrector in already:
                    continue
                already.add(corrector)
                added.append(corrector)
                meta[corrector] = {"inherited_rank": rank, "inherited_from": origin}
            if not correctors and not dropped:
                logger.info("纠正后补：被作废候选 %s 没有可用纠正者", origin)
        if added:
            logger.info(
                "纠正后补：%d 个被作废候选 → 补入 %d 条（追链超限丢弃 %d 条）",
                len(eligible),
                len(added),
                chain_dropped,
            )
        return {
            "ids": added,
            "meta": meta,
            "candidates": len(eligible),
            "chain_dropped": chain_dropped,
        }

    def _expand(
        self,
        parents: list[str],
        details: dict[str, dict],
        voided_set: set[str],
        *,
        max_items: int,
        max_chars: int,
        already: list[str],
    ) -> dict:
        """会话邻域展开：同 session_id ±2 条，不占名额、继承父项名次、过作废过滤。"""
        seen = set(already)
        candidates: list[tuple[int, int, str, str]] = []  # (父项名次, recorded_at, id, 父 id)
        for rank, parent in enumerate(parents, 1):
            detail = details.get(parent)
            if detail is None:
                continue
            rows = self.db.read_conn.execute(
                voided_mod.VOIDED_CTE
                + " SELECT m.id, m.recorded_at FROM memories m"
                " WHERE m.session_id = ? AND m.seq BETWEEN ? AND ? AND m.seq != ?"
                f" AND {voided_mod.NOT_VOIDED_SQL}"
                " ORDER BY m.recorded_at DESC, m.id ASC",
                (
                    detail["session_id"],
                    int(detail["seq"]) - EXPAND_WINDOW,
                    int(detail["seq"]) + EXPAND_WINDOW,
                    int(detail["seq"]),
                ),
            ).fetchall()
            for row in rows:
                mid = row["id"]
                if mid in seen:
                    continue
                if mid in voided_set:  # 双保险：SQL 已过滤
                    continue
                seen.add(mid)
                candidates.append((rank, int(row["recorded_at"]), mid, parent))
        candidates.sort(key=lambda item: (item[0], -item[1], item[2]))
        candidate_details = self._load_details([item[2] for item in candidates])
        char_budget = sum(
            len(truncate_for_output(details[mid]["content"], mid))
            for mid in already
            if mid in details
        )
        ids: list[str] = []
        parent_map: dict[str, str] = {}
        details_for_output: dict[str, dict] = {}
        truncated = False
        for _rank, _recorded_at, mid, parent in candidates:
            if len(already) + len(ids) >= max_items:
                truncated = True
                break
            detail = candidate_details.get(mid)
            if detail is None:
                continue
            content_len = len(truncate_for_output(detail["content"], mid))
            if char_budget + content_len > max_chars:
                truncated = True
                break
            char_budget += content_len
            ids.append(mid)
            parent_map[mid] = parent
            details_for_output[mid] = detail
        return {
            "ids": ids,
            "parents": parent_map,
            "details": details_for_output,
            "truncated": truncated,
        }


# ---------------------------------------------------------------- 纯函数

def _dedupe(ids: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for mid in ids:
        if mid not in seen:
            seen.add(mid)
            out.append(mid)
    return out


def _rrf_scores(
    route_lists: list[tuple[str, list[str]]], k: int
) -> tuple[dict[str, float], dict[str, dict[str, int]]]:
    scores: dict[str, float] = defaultdict(float)
    ranks: dict[str, dict[str, int]] = defaultdict(dict)
    for name, ids in route_lists:
        for rank, mid in enumerate(ids, 1):
            scores[mid] += 1.0 / (k + rank)
            ranks[mid][name] = rank
    return dict(scores), dict(ranks)


def _rank_candidates(
    scores: dict[str, float], details: dict[str, dict]
) -> list[str]:
    """固定 tie-break：RRF 分相同 → recorded_at 新者优先 → id 字典序（可复现）。"""
    return sorted(
        scores,
        key=lambda mid: (
            -scores[mid],
            -(details[mid]["recorded_at"] if mid in details else 0),
            mid,
        ),
    )


def _sources_for(memory_id: str, route_lists: list[tuple[str, list[str]]]) -> list[str]:
    sources: list[str] = []
    for name, ids in route_lists:
        if memory_id in ids and name not in sources:
            sources.append(name)
    return sources
