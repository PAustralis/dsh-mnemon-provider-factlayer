"""检索层（第二层，v8）：单混合列二元组路 + RRF + 路内替换 + 契约输出。

路线用**语义名**（vector / bigram / trigram / graph），配置与日志都不使用 R1..R6 编号。

v8 语义（docs/02 检索层设计）：

- **出厂默认 vector 单路**（2026-11-11 真实 embedding 重测：R@6 0.8729，优于
  bigram 0.4875，也优于等权 RRF 融合 0.6833）；单路**不做 RRF 求和**，直接按
  路线名次输出（score 仍为 ``1/(k+rank)``，契约数值语义不变）。
- **向量不可用时降级 bigram**（embedding 未配置 / provider 缺失 / 熔断 / 空索引 /
  查询异常）：本次查询追加 bigram 路、不融合，响应 ``degraded`` 置真，非空结果
  首条带 ``tags:["vector-unavailable"]``，空结果 hint 里列路状态。
- **各路取含 hidden 的原始 top-K**：SQL / mask 阶段不排除 hidden；排除只发生在出口。
- **路内替换（RRF 之前）**：命中 A ∈ hidden → 用 A 的 live head（可多个）占 A 的
  原始名次，A 移出该路；路内同 id 取最佳名次、每路至多一票；按
  ``(名次 asc, gseq desc)`` 排序后截断 ``ROUTE_TOP_K``。
- 跨路各一票是正当多路证据；被作废项只当位置提供者，不参与 RRF 打分。
- ``FINAL_K=4``、单条 ≤1200 字符、总 ≤4800 字符；每条结果带数值 ``score``
  （RRF，adapter 声明 ``scoreSemantics: provider-native``）与 ISO 8601 UTC ``createdAt``。
- 结果为空 → ``hint`` 以 ``NO_MATCH`` 开头 + 路状态摘要；结果非空 → 状态只走
  ``tags``（weak / via: / superseded-by: / hidden:N / vector-unavailable / expanded），
  被隐藏匹配计数另拼进首条 ``content`` 尾行。
- ``include_superseded`` 模型路径不可达，只由 admin 端点传入。
- 会话展开默认关；开启时只能占用 FINAL_K 名额内、过替换语义、继承名次。
- 出口断言：输出 ∩ hidden ⊆ 豁免集（仅 admin 显式请求）；否则丢弃 + 计数 + ERROR。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np

from . import textnorm
from . import voided as voided_mod
from .config import Config
from .db import Database
from .embeddings import (
    CIRCUIT_FAILURE_THRESHOLD,
    CIRCUIT_OPEN_SECONDS,
    CircuitBreaker,
    EmbeddingModelMismatch,
    EmbeddingProvider,
    VectorIndex,
    check_fingerprint,
    normalize_vector,
)

logger = logging.getLogger("factlayer.retrieval")

EVAL_META_KEY = "retrieval:eval"
R1_TIMEOUT_SECONDS = 3.0
# 与派生 embedding 失败的熔断共用同一实现与阈值（embeddings.CircuitBreaker）。
R1_CIRCUIT_FAILURES = CIRCUIT_FAILURE_THRESHOLD
R1_CIRCUIT_SECONDS = CIRCUIT_OPEN_SECONDS

# 契约状态 tag 白名单（docs/03 §4.4）；不参与写入语义
STATUS_TAG_PREFIXES = ("via:", "superseded-by:", "hidden:")
STATUS_TAGS = ("weak", "expanded", "vector-unavailable")


def iso_utc(recorded_at_ms: int) -> str:
    """Unix 毫秒 → ISO 8601 UTC 字符串（契约要求字符串，给数字会被静默丢弃）。"""
    dt = datetime.fromtimestamp(int(recorded_at_ms) / 1000.0, tz=timezone.utc)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def truncate_item(content: str, memory_id: str, max_chars: int) -> str:
    """把单条 content 截到 ≤max_chars（含截断自报标记）。"""
    if len(content) <= max_chars:
        return content
    marker = f"[已截断，原文 {len(content)} 字，id={memory_id}]"
    keep = max(0, max_chars - len(marker))
    return content[:keep] + marker


@dataclass
class RouteOutcome:
    """单路一次查询的结果；raw_ids 含 hidden，ids 是路内替换/去重/截断后的列表。"""

    name: str
    ids: list[str] = field(default_factory=list)
    ranks: list[int] = field(default_factory=list)
    raw_ids: list[str] = field(default_factory=list)
    hidden_hits: list[tuple[str, str]] = field(default_factory=list)
    error: str | None = None
    skipped: str | None = None
    unavailable: bool = False
    token_count: int = 0

    @property
    def raw_count(self) -> int:
        return len(self.raw_ids)


class RetrievalStats:
    """每路最近空结果率 + 事件计数（/health 暴露，进程内滚动窗口）。"""

    ROUTES = ("vector", "bigram", "trigram", "graph")

    def __init__(self, window: int = 200) -> None:
        self._lock = threading.Lock()
        self._window = window
        self.route_queries = {name: 0 for name in self.ROUTES}
        self.route_empty = {name: 0 for name in self.ROUTES}
        self.route_errors = {name: 0 for name in self.ROUTES}
        self.route_unavailable = {name: 0 for name in self.ROUTES}
        self._recent_empty = {name: deque(maxlen=window) for name in self.ROUTES}
        self.queries = 0
        self.no_match_count = 0
        self.hidden_replaced_count = 0
        self.exit_assertion_drops = 0
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
                self.hidden_replaced_count += len(outcome.hidden_hits)

    def record_no_match(self) -> None:
        with self._lock:
            self.no_match_count += 1

    def record_exit_drop(self, count: int) -> None:
        with self._lock:
            self.exit_assertion_drops += count

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
                "no_match_count": self.no_match_count,
                "hidden_replaced_count": self.hidden_replaced_count,
                "exit_assertion_drops": self.exit_assertion_drops,
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
    """生效路线 + 自动降级状态（保留第一层能力；v8 默认只有 bigram，不触发）。"""
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
        # R1 熔断：连续失败 3 次 → 打开 5 分钟（CircuitBreaker 与派生层共用）
        self._vector_circuit = CircuitBreaker(
            threshold=R1_CIRCUIT_FAILURES,
            open_seconds=R1_CIRCUIT_SECONDS,
            label="vector 路",
        )
        self._canary_history: deque[dict] = deque(maxlen=2)
        self._canary_last: dict | None = None

    def invalidate_caches(self) -> None:
        self.index.invalidate()

    # ------------------------------------------------------------------ 单路
    def _fts_rank(
        self, table: str, expr: str | None, limit: int, *, gseq_desc: bool = True
    ) -> list[str]:
        """原始 top-K（**含 hidden**，SQL 不排除作废）。

        bigram 路沿用基线的 ``bm25 ASC, rowid ASC``（rowid == gseq，天然升序）：
        BM25 同分并列若改成 gseq DESC 会翻转基线 top-6 / R@6（见测试与报告）。
        其余路与所有输出 tie-break 仍用 ``gseq DESC``（新者优先）。
        """
        if not expr:
            return []
        tie = "m.gseq DESC" if gseq_desc else "m.gseq ASC"
        sql = (
            f"SELECT m.id FROM {table} f"
            f" JOIN memories m ON m.gseq = f.rowid"
            f" WHERE {table} MATCH ?"
            f" ORDER BY bm25({table}) ASC, {tie} LIMIT ?"
        )
        rows = self.db.read_conn.execute(sql, (expr, int(limit))).fetchall()
        return [row[0] for row in rows]

    def _gseq_map(self, ids: list[str]) -> dict[str, int]:
        if not ids:
            return {}
        unique = list(dict.fromkeys(ids))
        placeholders = ",".join("?" * len(unique))
        rows = self.db.read_conn.execute(
            f"SELECT id, gseq FROM memories WHERE id IN ({placeholders})", unique
        ).fetchall()
        return {row["id"]: int(row["gseq"]) for row in rows}

    def bigram_route(self, query: str, limit: int) -> RouteOutcome:
        """R2 二元组：与基线同构的单混合列查询（无单字回退、无最近兜底）。"""
        tokens, _truncated = textnorm.query_tokens(query)
        expr = textnorm.fts_match_expr(tokens)
        if not expr:
            return RouteOutcome(name="bigram", skipped="no_tokens", token_count=0)
        raw = self._fts_rank("memories_fts_bigram", expr, limit, gseq_desc=False)
        return RouteOutcome(name="bigram", raw_ids=raw, token_count=len(tokens))

    def trigram_route(self, query: str, limit: int) -> RouteOutcome:
        """可选 trigram 路（v8 默认不启用）；1–2 字查询在此路不可用。"""
        tokens = textnorm.trigram_tokens(query)
        expr = textnorm.fts_match_expr(tokens)
        if not expr:
            return RouteOutcome(name="trigram", skipped="query_shorter_than_3_chars")
        raw = self._fts_rank("memories_fts_trigram", expr, limit)
        return RouteOutcome(name="trigram", raw_ids=raw, token_count=len(tokens))

    def graph_route(self, query: str, limit: int) -> RouteOutcome:
        """图邻居路：只留接口与开关，默认关；未实现时明确返回空并计数。"""
        logger.warning(
            "graph 路线已启用但本层未实现（FalkorDB 实体 1 跳邻居）；"
            "本次返回空结果并计入 /health.retrieval.routes.graph.unavailable"
        )
        return RouteOutcome(
            name="graph", ids=[], unavailable=True, skipped="graph_not_implemented"
        )

    def vector_route(self, query: str, limit: int) -> RouteOutcome:
        """R1 向量：原始 top-K（**含 hidden**，不预先 mask）。

        - ``EmbeddingModelMismatch`` 向上抛（配置事故 → 409）。
        - 其他异常计数并向上抛，由 search() 降级；连续 3 次失败熔断 5 分钟。
        """
        if self.embedding_provider is None:
            return RouteOutcome(
                name="vector", unavailable=True, skipped="embedding_provider_missing"
            )
        if not self._vector_circuit.allow():
            return RouteOutcome(
                name="vector", unavailable=True, skipped="circuit_open"
            )
        check_fingerprint(self.db, self.embedding_provider, self.cfg.embedding_dim_int)
        dim = self.cfg.embedding_dim_int
        ids, matrix, gseqs = self.index.load(
            self.db, model_id=self.embedding_provider.model_id, dim=dim
        )
        if not ids or matrix.size == 0:
            return RouteOutcome(name="vector", skipped="empty_embedding_index")
        try:
            query_vectors = self.embedding_provider.embed([textnorm.normalize(query)])
        except Exception as exc:
            self._record_vector_failure(exc)
            raise
        if not query_vectors:
            exc = RuntimeError("embedding provider 返回空结果")
            self._record_vector_failure(exc)
            raise exc
        query_vector = normalize_vector(query_vectors[0])
        if query_vector.shape[0] != matrix.shape[1]:
            raise EmbeddingModelMismatch(
                f"查询向量维度 {query_vector.shape[0]} 与库内矩阵维度 "
                f"{matrix.shape[1]} 不符；请 POST /admin/rebuild"
            )
        self._record_vector_success()
        sims = matrix @ query_vector
        # 确定性排序：相似度降序 → gseq DESC
        order = sorted(
            range(len(ids)), key=lambda i: (-float(sims[i]), -int(gseqs[i]))
        )
        raw = [ids[i] for i in order[: int(limit)]]
        return RouteOutcome(
            name="vector",
            raw_ids=raw,
            token_count=len(textnorm.query_tokens(query)[0]),
        )

    def _record_vector_failure(self, exc: Exception) -> None:
        self._vector_circuit.record_failure(exc)
        last_error = self._vector_circuit.state()["last_error"]
        self.stats.record_vector_degraded(last_error or str(exc))

    def _record_vector_success(self) -> None:
        self._vector_circuit.record_success()

    def vector_state(self) -> dict:
        return self._vector_circuit.state()

    # ------------------------------------------------------------------ 主流程
    def search(
        self,
        query: str,
        *,
        session_id: str | None = None,
        limit: int | None = None,
        include_superseded: bool = False,
    ) -> dict:
        started = time.perf_counter()
        query_id = uuid.uuid4().hex
        timings: dict[str, float] = {}
        norm_query = textnorm.normalize(query or "")
        if not norm_query:
            raise ValueError("query 不能为空")
        final_k = int(self.cfg.final_k)
        if limit is not None:
            final_k = max(1, min(final_k, int(limit)))
        route_limit = int(self.cfg.route_top_k)
        degrade = dict(self.degrade)

        tokens, token_truncated = textnorm.query_tokens(norm_query)
        snapshot = voided_mod.hidden_snapshot(self.db.read_conn)
        hidden_matched_ids: set[str] = set()

        if not tokens:
            return self._no_match_response(
                query,
                session_id,
                query_id,
                started,
                outcomes=[],
                token_count=0,
                token_truncated=0,
                hidden_size=len(snapshot.hidden),
                reason="tokens=0（查询只含标点/空白等不产 token 的字符）",
            )

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
                outcome = RouteOutcome(
                    name=name, ids=[], error=f"{type(exc).__name__}: {exc}"
                )
            finally:
                timings[f"route_{name}"] = round(
                    (time.perf_counter() - route_started) * 1000, 3
                )
            outcomes.append(outcome)

        # ---- 向量单路降级：不融合，直接追加 bigram 结果 ----------------------
        # 触发条件：vector 路不可用（未配置 provider / 熔断 / 空索引 / 查询异常）。
        # 只有 ``routes == ("vector",)`` 时降级；显式配置多路仍按原 RRF 融合。
        if tuple(self.routes) == ("vector",) and outcomes:
            vector_outcome = outcomes[0]
            fallback_reason = (
                vector_outcome.error
                or vector_outcome.skipped
                or ("unavailable" if vector_outcome.unavailable else None)
            )
            if fallback_reason:
                logger.warning(
                    "vector 单路不可用（%s），本次查询降级 bigram", fallback_reason
                )
                fallback_started = time.perf_counter()
                try:
                    fallback_outcome = self.bigram_route(norm_query, route_limit)
                except Exception as exc:  # pragma: no cover - FTS 正常不会失败
                    logger.exception("bigram 降级路失败")
                    fallback_outcome = RouteOutcome(
                        name="bigram", error=f"{type(exc).__name__}: {exc}"
                    )
                timings["route_bigram_fallback"] = round(
                    (time.perf_counter() - fallback_started) * 1000, 3
                )
                outcomes.append(fallback_outcome)
                degrade = {
                    "degraded": True,
                    "reason": f"vector 不可用（{fallback_reason}），已降级 bigram",
                }

        # ---- 路内替换（RRF 之前） -------------------------------------------
        repl_started = time.perf_counter()
        all_raw = [mid for o in outcomes for mid in o.raw_ids]
        head_ids: set[str] = set()
        cycle_map: dict[str, str] = {}
        if not include_superseded:
            for outcome in outcomes:
                for mid in outcome.raw_ids:
                    if mid in snapshot.hidden:
                        head_ids.update(snapshot.heads.get(mid, ()))
        # 环整体只算一票：每个连通分量选 gseq 最新的成员作代表（其余成员稍后展开）
        cycle_members = [mid for comp in snapshot.cycle_components for mid in comp]
        gseq_map = self._gseq_map(all_raw + list(head_ids) + cycle_members)
        for comp in snapshot.cycle_components:
            representative = max(comp, key=lambda mid: gseq_map.get(mid, 0))
            for member in comp:
                cycle_map[member] = representative
        for outcome in outcomes:
            if include_superseded:
                outcome.ids = _dedupe(outcome.raw_ids)[:route_limit]
                outcome.ranks = list(range(1, len(outcome.ids) + 1))
            else:
                outcome.ids, outcome.ranks, outcome.hidden_hits = (
                    _apply_route_replacement(
                        outcome.raw_ids,
                        snapshot.hidden,
                        snapshot.heads,
                        gseq_map,
                        route_limit,
                        cycle_map,
                    )
                )
                for hidden_id, _head in outcome.hidden_hits:
                    hidden_matched_ids.add(hidden_id)
        timings["replacement"] = round(
            (time.perf_counter() - repl_started) * 1000, 3
        )
        if not include_superseded:
            self.stats.record_query(outcomes)
        else:
            # admin 查询不计入模型路径统计
            pass

        # ---- RRF：score(d) = Σ 1/(k + rank_i) --------------------------------
        fuse_started = time.perf_counter()
        route_lists = [
            (o.name, o.ids, o.ranks) for o in outcomes if o.ids
        ]
        rrf_k = int(self.cfg.rrf_k)
        if len(route_lists) == 1:
            # 单路不融合：直接按该路名次定序（score 保持 1/(k+rank)，契约不变）。
            name, ids, route_ranks = route_lists[0]
            scores = {}
            ranks = {}
            for position, mid in enumerate(ids):
                rank = (
                    int(route_ranks[position])
                    if position < len(route_ranks)
                    else position + 1
                )
                scores[mid] = 1.0 / (rrf_k + rank)
                ranks[mid] = {name: rank}
        else:
            scores, ranks = _rrf_scores(route_lists, rrf_k)
        details = self._load_details(list(scores))
        order = _rank_candidates(scores, gseq_map, details)
        fused_count = len(order)
        timings["fusion"] = round((time.perf_counter() - fuse_started) * 1000, 3)

        selected = order[:final_k]
        cycle_tails: dict[str, str] = {}
        if not include_superseded:
            selected, cycle_tails = self._expand_cycles(
                selected, snapshot, gseq_map, details, scores, ranks, final_k
            )

        # ---- 会话展开（默认关；只能占用 FINAL_K 名额内） --------------------
        expand_started = time.perf_counter()
        expansion = {"ids": [], "parents": {}, "details": {}, "truncated": 0}
        if self.cfg.expand_enabled and len(selected) < final_k:
            expansion = self._expand(
                selected,
                details,
                snapshot,
                gseq_map,
                already=selected,
                final_k=final_k,
            )
            details = {**details, **expansion["details"]}
        timings["expansion"] = round(
            (time.perf_counter() - expand_started) * 1000, 3
        )
        if expansion["ids"]:
            selected = selected + expansion["ids"]
            # 展开项继承父项名次（不重新打分）：score 用父项 RRF 分，但不再算 via 票
            for mid, parent in expansion["parents"].items():
                scores[mid] = scores.get(parent, 0.0)

        # ---- 出口断言：输出 ∩ hidden ⊆ 豁免集（admin 显式请求） --------------
        if not include_superseded:
            leaked = [mid for mid in selected if mid in snapshot.hidden]
            if leaked:
                self.stats.record_exit_drop(len(leaked))
                logger.error(
                    "检索出口断言失败：query_id=%s query=%r hidden 泄漏 %s",
                    query_id,
                    norm_query,
                    leaked,
                )
                selected = [mid for mid in selected if mid not in snapshot.hidden]

        # ---- 组装契约输出（score / createdAt / tags / 字符预算） --------------
        if include_superseded:
            # admin 显式请求：输出里的 hidden 成员也计入 N（且豁免出口断言）
            hidden_matched_ids.update(mid for mid in selected if mid in snapshot.hidden)
        hidden_count = len(hidden_matched_ids)
        replaced_by: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for outcome in outcomes:
            for hidden_id, head in outcome.hidden_hits:
                replaced_by[head].append((hidden_id, outcome.name))
        hidden_notice = (
            f"〔另有 {hidden_count} 条相关记录已被纠正，未显示〕"
            if hidden_count and not include_superseded
            else None
        )
        items, truncated_items, chars = self._build_items(
            selected,
            details,
            scores,
            ranks,
            gseq_map,
            replaced_by,
            expansion,
            hidden_count,
            hidden_notice,
            snapshot,
            cycle_tails,
            hidden_visible=include_superseded,
            vector_unavailable=any(
                o.name == "vector" and (o.error or o.unavailable or o.skipped)
                for o in outcomes
            ),
        )

        if truncated_items:
            notice = f"〔另有 {truncated_items} 条因字符预算未显示〕"
            # 尾行走**单条预算**（1200），不能只看总预算 4800：首条可能已有
            # hidden/环尾行，两条尾行叠加会超过单条上限，被下游按 1200 硬截时
            # 通知会被截掉（v8 审阅第 4 条）。
            items = _append_tail_line(items, notice, int(self.cfg.item_max_chars))
            chars = sum(len(item["content"]) for item in items)

        if not items:
            hint = self._route_hint(
                outcomes, tokens, hidden_count, token_truncated, degrade=degrade
            )
            self.stats.record_no_match()
            response = self._base_response(
                query,
                session_id,
                query_id,
                started,
                outcomes,
                final_k,
                degrade=degrade,
            )
            response.update(
                {
                    "count": 0,
                    "items": [],
                    "hint": hint,
                    "hidden": {
                        "size": len(snapshot.hidden),
                        "matched": hidden_count,
                    },
                    "token_count": len(tokens),
                    "token_truncated": token_truncated,
                }
            )
            logger.info(
                "search no_match %s",
                json.dumps(
                    {"query_id": query_id, "query_len": len(norm_query), "hint": hint},
                    ensure_ascii=False,
                ),
            )
            return response

        response = self._base_response(
            query,
            session_id,
            query_id,
            started,
            outcomes,
            final_k,
            degrade=degrade,
        )
        response.update(
            {
                "count": len(items),
                "items": items,
                "hint": None,
                "hidden": {"size": len(snapshot.hidden), "matched": hidden_count},
                "token_count": len(tokens),
                "token_truncated": token_truncated,
                "output": {
                    "max_items": final_k,
                    "max_chars": int(self.cfg.total_max_chars),
                    "item_max_chars": int(self.cfg.item_max_chars),
                    "chars": chars,
                    "truncated_items": truncated_items,
                },
            }
        )
        timings["total"] = round((time.perf_counter() - started) * 1000, 3)
        logger.info(
            "search %s",
            json.dumps(
                {
                    "event": "search",
                    "query_id": query_id,
                    "query_len": len(norm_query),
                    "routes_enabled": list(self.routes),
                    "route_raw": {o.name: o.raw_count for o in outcomes},
                    "route_kept": {o.name: len(o.ids) for o in outcomes},
                    "hidden_matched": hidden_count,
                    "fused": fused_count,
                    "final": len(items),
                    "include_superseded": include_superseded,
                    "elapsed_ms": timings,
                },
                ensure_ascii=False,
            ),
        )
        return response

    # ------------------------------------------------------------------ 组装
    def _base_response(
        self,
        query: str,
        session_id: str | None,
        query_id: str,
        started: float,
        outcomes: list[RouteOutcome],
        final_k: int,
        degrade: dict | None = None,
    ) -> dict:
        return {
            "query": query,
            "query_id": query_id,
            "session_id": session_id,
            "count": 0,
            "items": [],
            "limit": final_k,
            "routes": {
                o.name: {
                    "raw": o.raw_count,
                    "kept": len(o.ids),
                    "skipped": o.skipped,
                    "error": o.error,
                    "unavailable": o.unavailable,
                    "tokens": o.token_count,
                    "hidden_replaced": len(o.hidden_hits),
                }
                for o in outcomes
            },
            "degraded": self.degrade if degrade is None else degrade,
            "elapsed_ms": {"total": round((time.perf_counter() - started) * 1000, 3)},
        }

    def _build_items(
        self,
        selected: list[str],
        details: dict[str, dict],
        scores: dict[str, float],
        ranks: dict[str, dict[str, int]],
        gseq_map: dict[str, int],
        replaced_by: dict[str, list[tuple[str, str]]],
        expansion: dict,
        hidden_count: int,
        hidden_notice: str | None,
        snapshot: voided_mod.HiddenSnapshot,
        cycle_tails: dict[str, str],
        *,
        hidden_visible: bool,
        vector_unavailable: bool,
    ) -> tuple[list[dict], int, int]:
        final_k = int(self.cfg.final_k)
        item_max = int(self.cfg.item_max_chars)
        total_max = int(self.cfg.total_max_chars)
        expanded_set = set(expansion["ids"])
        parent_rank = {
            parent: rank
            for rank, parent in enumerate(
                [mid for mid in selected if mid not in expanded_set], 1
            )
        }
        items: list[dict] = []
        chars = 0
        truncated_items = 0
        for position, mid in enumerate(selected):
            detail = details.get(mid)
            if detail is None:
                continue
            content = truncate_item(detail["content"], mid, item_max)
            tags: list[str] = []
            route_names = sorted(ranks.get(mid, {}))
            for name in route_names:
                tags.append(f"via:{name}")
            origins = replaced_by.get(mid, [])
            if origins:
                # 一个存活纠正者可继承多个被作废项的名次；tag 上限内全部标出
                for hidden_id, _route in origins:
                    tags.append(f"superseded-by:{hidden_id}")
            if mid in expanded_set:
                tags.append("expanded")
            if mid in snapshot.cycle_members:
                tags.append("cycle")  # 循环裁决：不可达存活节点，不 hidden
            if mid in snapshot.overlong_chain:
                tags.append("chain-too-long")  # 20 跳触顶：不丢弃，显式标注
            best_rank = min(ranks.get(mid, {}).values()) if ranks.get(mid) else None
            if mid not in expanded_set and (
                best_rank is None or best_rank > int(self.cfg.weak_best_rank)
            ):
                tags.append("weak")
            if position == 0:
                if hidden_count and not hidden_visible:
                    tags.append(f"hidden:{hidden_count}")
                if vector_unavailable:
                    tags.append("vector-unavailable")
            tags = _normalize_tags(tags)
            if position == 0 and hidden_notice and not hidden_visible:
                content = _ensure_tail(content, hidden_notice, item_max)
            if mid in cycle_tails:
                content = _ensure_tail(content, cycle_tails[mid], item_max)
            parent = expansion["parents"].get(mid)
            item = {
                "id": mid,
                "content": content,
                "createdAt": iso_utc(detail["recorded_at"]),
                "recorded_at": int(detail["recorded_at"]),
                "score": round(float(scores.get(mid, 0.0)), 12),
                "tags": tags,
                "routes": route_names,
                "content_truncated": len(detail["content"]) > len(content),
                "session_id": detail["session_id"],
                "seq": int(detail["seq"]),
                "expanded": mid in expanded_set,
                "expanded_from": parent,
                "inherited_rank": parent_rank.get(parent) if parent else None,
            }
            if chars + len(content) > total_max:
                truncated_items = len(selected) - position
                break
            chars += len(content)
            items.append(item)
            if len(items) >= final_k:
                truncated_items = max(0, len(selected) - position - 1)
                break
        return items, truncated_items, chars

    def _expand_cycles(
        self,
        selected: list[str],
        snapshot: voided_mod.HiddenSnapshot,
        gseq_map: dict[str, int],
        details: dict[str, dict],
        scores: dict[str, float],
        ranks: dict[str, dict[str, int]],
        final_k: int,
    ) -> tuple[list[str], dict[str, str]]:
        """历史环：环整体只算一票；正文最多展示 3 个最新成员，其余列 id 清单。

        - 代表（gseq 最新）在路内替换阶段占住票；这里把代表展开成最多 3 个成员。
        - 同一环的成员共享代表的名次 / RRF 分（不再重复计票）。
        - 超出 3 个或被 FINAL_K 挤出的成员，id 清单拼进最后一个已展示成员的尾行。
        """
        comp_of: dict[str, list[str]] = {}
        for comp in snapshot.cycle_components:
            for member in comp:
                comp_of[member] = comp
        if not comp_of:
            return selected, {}
        expanded: list[str] = []
        handled: set[tuple[str, ...]] = set()
        tails: dict[str, str] = {}
        for mid in selected:
            comp = comp_of.get(mid)
            key = tuple(comp) if comp is not None else ()
            if comp is None or key in handled:
                expanded.append(mid)
                continue
            handled.add(key)
            members = sorted(comp, key=lambda m: -gseq_map.get(m, 0))
            room = max(0, final_k - len(expanded))
            take = members[: min(3, room)]
            for member in take:
                if member not in details:
                    details.update(self._load_details([member]))
                # 共享代表的名次与分数：环整体只算一票
                scores[member] = scores.get(mid, 0.0)
                ranks[member] = dict(ranks.get(mid, {}))
                expanded.append(member)
            omitted = [m for m in members if m not in take]
            if omitted and take:
                tails[take[-1]] = "〔循环裁决：同一环内其他成员 " + "、".join(omitted) + "〕"
        return expanded[:final_k], tails

    def _no_match_response(
        self,
        query: str,
        session_id: str | None,
        query_id: str,
        started: float,
        *,
        outcomes: list[RouteOutcome],
        token_count: int,
        token_truncated: int,
        hidden_size: int,
        reason: str,
        degrade: dict | None = None,
    ) -> dict:
        self.stats.record_no_match()
        hint = self._route_hint(
            outcomes,
            [""] * token_count,
            0,
            token_truncated,
            reason=reason,
            degrade=degrade,
        )
        response = self._base_response(
            query,
            session_id,
            query_id,
            started,
            outcomes,
            int(self.cfg.final_k),
            degrade=degrade,
        )
        response.update(
            {
                "count": 0,
                "items": [],
                "hint": hint,
                "hidden": {"size": hidden_size, "matched": 0},
                "token_count": token_count,
                "token_truncated": token_truncated,
            }
        )
        logger.info(
            "search no_match %s",
            json.dumps({"query_id": query_id, "hint": hint}, ensure_ascii=False),
        )
        return response

    def _route_hint(
        self,
        outcomes: list[RouteOutcome],
        tokens: list[str],
        hidden_count: int,
        token_truncated: int,
        *,
        reason: str | None = None,
        degrade: dict | None = None,
    ) -> str:
        """空结果 hint：必须以 NO_MATCH 开头 + 参与/降级路 + token 数。"""
        statuses: list[str] = []
        for outcome in outcomes:
            if outcome.error:
                statuses.append(f"{outcome.name}(error:{outcome.error})")
            elif outcome.unavailable:
                statuses.append(f"{outcome.name}(unavailable:{outcome.skipped})")
            elif outcome.skipped:
                statuses.append(f"{outcome.name}(skipped:{outcome.skipped})")
            else:
                statuses.append(f"{outcome.name}(ok)")
        routes = "+".join(statuses) if statuses else "none"
        parts = [
            "NO_MATCH",
            f"routes={routes}",
            f"tokens={len(tokens)}" + (f"(+{token_truncated} 截断)" if token_truncated else ""),
            f"hidden={hidden_count}",
        ]
        active_degrade = self.degrade if degrade is None else degrade
        if active_degrade.get("degraded"):
            parts.append(f"degraded={active_degrade.get('reason')}")
        if reason:
            parts.append(f"reason={reason}")
        return "｜".join(parts)

    # ------------------------------------------------------------------ 会话展开
    def _expand(
        self,
        parents: list[str],
        details: dict[str, dict],
        snapshot: voided_mod.HiddenSnapshot,
        gseq_map: dict[str, int],
        *,
        already: list[str],
        final_k: int,
    ) -> dict:
        """同 session ±expand_window 条；过替换语义、继承父项名次、不占额外名额。"""
        seen = set(already)
        window = int(self.cfg.expand_window)
        candidates: list[tuple[int, str, str]] = []  # (父名次, id, 父 id)
        for rank, parent in enumerate(parents, 1):
            detail = details.get(parent)
            if detail is None:
                continue
            rows = self.db.read_conn.execute(
                "SELECT id, gseq FROM memories WHERE session_id=? AND seq BETWEEN ? AND ? "
                "AND id != ? ORDER BY gseq DESC",
                (
                    detail["session_id"],
                    int(detail["seq"]) - window,
                    int(detail["seq"]) + window,
                    parent,
                ),
            ).fetchall()
            for row in rows:
                mid = row["id"]
                if mid in snapshot.hidden:
                    for head in snapshot.heads.get(mid, ()):
                        if head not in seen:
                            seen.add(head)
                            candidates.append((rank, head, parent))
                elif mid not in seen:
                    seen.add(mid)
                    candidates.append((rank, mid, parent))
        candidates.sort(key=lambda item: (item[0], -gseq_map.get(item[1], 0)))
        ids: list[str] = []
        parents_map: dict[str, str] = {}
        details_for_output: dict[str, dict] = {}
        truncated = 0
        remaining = max(0, final_k - len(already))
        for rank, mid, parent in candidates:
            if len(ids) >= remaining:
                truncated += 1
                continue
            detail = details.get(mid) or self._load_details([mid]).get(mid)
            if detail is None:
                continue
            ids.append(mid)
            parents_map[mid] = parent
            details_for_output[mid] = detail
        return {
            "ids": ids,
            "parents": parents_map,
            "details": details_for_output,
            "truncated": truncated,
        }

    # ------------------------------------------------------------------ 详情
    def _load_details(self, ids: list[str]) -> dict[str, dict]:
        if not ids:
            return {}
        unique = list(dict.fromkeys(ids))
        placeholders = ",".join("?" * len(unique))
        rows = self.db.read_conn.execute(
            "SELECT id, content, recorded_at, session_id, seq, gseq FROM memories "
            f"WHERE id IN ({placeholders})",
            unique,
        ).fetchall()
        return {
            row["id"]: {
                "id": row["id"],
                "content": row["content"],
                "recorded_at": int(row["recorded_at"]),
                "session_id": row["session_id"],
                "seq": int(row["seq"]),
                "gseq": int(row["gseq"]),
            }
            for row in rows
        }

    # ------------------------------------------------------------------ canary
    def run_canary(self) -> dict:
        """金丝雀：抽单 CJK 段 ≥6 字的子串，对 bigram 路严格断言（不计统计）。"""
        from .canary import run_canary

        report = run_canary(self, self.cfg)
        self._canary_last = report
        self._canary_history.append(report)
        rates = [
            report["failure_rate"],
            *[
                item["failure_rate"]
                for item in list(self._canary_history)[:-1]
            ],
        ]
        alert = len(rates) >= 2 and all(
            r is not None and r >= float(self.cfg.canary_alert_rate)
            for r in rates[:2]
        )
        report["alert"] = bool(alert)
        return report

    def canary_state(self) -> dict:
        return self._canary_last or {"status": "not_run", "alert": False}


# ---------------------------------------------------------------- 纯函数

def _dedupe(ids: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for mid in ids:
        if mid not in seen:
            seen.add(mid)
            out.append(mid)
    return out


def _apply_route_replacement(
    raw_ids: list[str],
    hidden: set[str],
    heads: dict[str, list[str]],
    gseq_map: dict[str, int],
    limit: int,
    cycle_map: dict[str, str] | None = None,
) -> tuple[list[str], list[int], list[tuple[str, str]]]:
    """路内替换：hidden 命中 → live heads 占原始名次；同 id 取最佳名次。

    - ``cycle_map``：历史环成员 → 代表（gseq 最新），环整体在路内只保留一票。
    - 返回 (替换/去重后的 id 列表, 与 ids 平行的名次列表, [(被作废 id, head)])。
      多个存活纠正者**并列继承** A 的同一个名次（不是各占一名）。
    """
    cycle_map = cycle_map or {}
    entries: list[tuple[int, int, str]] = []  # (原始名次, gseq, id)
    hidden_hits: list[tuple[str, str]] = []
    for rank, mid in enumerate(raw_ids, 1):
        if mid in hidden:
            for head in heads.get(mid, ()):
                entries.append((rank, int(gseq_map.get(head, 0)), head))
                hidden_hits.append((mid, head))
        elif mid in cycle_map:
            rep = cycle_map[mid]
            entries.append((rank, int(gseq_map.get(rep, 0)), rep))
        else:
            entries.append((rank, int(gseq_map.get(mid, 0)), mid))
    # 名次 asc → gseq DESC（新者优先）
    entries.sort(key=lambda item: (item[0], -item[1]))
    seen: set[str] = set()
    ids: list[str] = []
    ranks: list[int] = []
    for rank, _gseq, mid in entries:
        if mid in seen:
            continue
        seen.add(mid)
        ids.append(mid)
        ranks.append(rank)
        if len(ids) >= int(limit):
            break
    return ids, ranks, hidden_hits


def _rrf_scores(
    route_lists,
    k: int,
) -> tuple[dict[str, float], dict[str, dict[str, int]]]:
    """RRF：每个 id 每路至多一票；route_lists 项可为 (name, ids) 或
    (name, ids, ranks)，ranks 缺省按位置 1..n。"""
    scores: dict[str, float] = defaultdict(float)
    ranks: dict[str, dict[str, int]] = defaultdict(dict)
    for entry in route_lists:
        name, ids = entry[0], entry[1]
        rank_list = entry[2] if len(entry) > 2 else None
        for position, mid in enumerate(ids):
            rank = (
                int(rank_list[position])
                if rank_list is not None and position < len(rank_list)
                else position + 1
            )
            scores[mid] += 1.0 / (k + rank)
            ranks[mid][name] = min(ranks[mid].get(name, rank), rank)
    return dict(scores), dict(ranks)


def _rank_candidates(
    scores: dict[str, float],
    gseq_map: dict[str, int],
    details: dict[str, dict],
) -> list[str]:
    """固定 tie-break：RRF 分相同 → **gseq 大者优先**（新者优先）。"""

    def gseq(mid: str) -> int:
        if mid in gseq_map:
            return gseq_map[mid]
        detail = details.get(mid)
        return int(detail["gseq"]) if detail else 0

    return sorted(scores, key=lambda mid: (-scores[mid], -gseq(mid)))


def _normalize_tags(tags: list[str]) -> list[str]:
    """状态 tag：不用逗号、去重保序、上限 8 个、每个 ≤300 字符。"""
    out: list[str] = []
    for tag in tags:
        clean = str(tag).replace(",", ":").replace("，", ":")[:300]
        if clean and clean not in out:
            out.append(clean)
    return out[:8]


def _ensure_tail(content: str, notice: str, max_chars: int) -> str:
    """把 notice 作为尾行拼进 content，**保证**总长 ≤max_chars。"""
    max_chars = max(1, int(max_chars))
    # 极端情况 notice 本身超预算：先截尾行，否则 keep=0 也会返回超长串
    tail = ("\n" + notice)[:max_chars]
    if len(content) + len(tail) <= max_chars:
        return content + tail
    keep = max(0, max_chars - len(tail))
    return content[:keep] + tail


def _append_tail_line(
    items: list[dict], notice: str, item_max_chars: int
) -> list[dict]:
    """把 notice 作为尾行拼到首条 ``content``，**按单条预算**保证 ≤item_max_chars。

    与 ``_ensure_tail`` 同一约束（统一入口），避免首条先有 hidden/环尾行、再叠
    预算尾行时突破 1200。
    """
    if not items:
        return items
    first = items[0]
    first["content"] = _ensure_tail(first["content"], notice, item_max_chars)
    return items
