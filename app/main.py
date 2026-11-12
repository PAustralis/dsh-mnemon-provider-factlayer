"""FastAPI 装配（工厂模式，import 无副作用）。

端点：POST /memories、GET /memories/{id}、GET /memories（按 session）、
POST /supersedes、GET /health、POST /search（检索层）、GET /spaces（对接层
discover），以及运维辅助 POST /derive/tick、POST /admin/rebuild、
POST /admin/backup、GET /admin/search、POST /admin/canary。部署固定单 worker：

    python -m app.main
    python -m uvicorn app.main:create_app --factory --workers 1

admin 端点由 ``ADMIN_TOKEN`` 保护：非空时要求 ``X-Admin-Token`` 或
``Authorization: Bearer <token>``；为空时只接受回环请求（含 TestClient）。
``include_superseded`` 在模型路径上不可达，只由 ``GET /admin/search`` 提供。

第三层（docs/06 v3）的三条硬约束：

1. ``GET /spaces`` 只读配置，O(1)，不查库（discover 的 externalId 来源）。
2. ``GET /health`` 只读后台快照（``HealthCache``，每 5s 刷新），请求里不跑聚合；
   快照未就绪时返回 ``{"status": "starting"}``。
3. ``POST /admin/rebuild`` 期间置 rebuilding 标志，``POST /search``、``/memories``
   （读与写）、``/admin/search`` 与 ``/supersedes``（写边）一律 503 ``rebuilding``，
   绝不返回部分结果；``/health`` 与 ``/spaces`` 仍 O(1) 可读。

模块级不创建 ``app``：``import app.main`` 不会建库、不会产生 facts.db。
**出厂默认 vector 单路**（docs/02 §2 / §9.3）：embedding 已配置但无效时仍在
``create_app`` 报错；**未配置**（EMBEDDING_MODEL / EMBEDDING_BASE_URL 为空）时不再
拒绝启动，检索在每次查询时降级 bigram，并在 ``degraded`` / ``vector-unavailable``
tag / NO_MATCH hint 里标明（docs/04）。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from typing import Callable

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .backup import BackupScheduler
from .config import Config, load_config
from .db import CHECKPOINT_MODES, Database
from .derive import DerivePipeline
from .embeddings import (
    EmbeddingConfigError,
    EmbeddingModelMismatch,
    EmbeddingProvider,
    VectorIndex,
    build_provider,
)
from .health import health_report
from .health_cache import HealthCache
from .index_text import INDEX_TEXT_REBUILD_KEY, INDEX_TEXT_VERSION
from .maintenance import Maintenance, optimize_fts
from .rebuild_gate import (
    REBUILDING_ERROR,
    REBUILDING_MESSAGE,
    RebuildGate,
    RebuildingInProgress,
)
from .retrieval import RetrievalService
from .schema import (
    get_index_text_version,
    index_text_rebuild_needed,
    init_db,
    set_meta,
)
from .write_path import create_memory, record_supersede
from . import read_path

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("factlayer.main")

DERIVE_LOOP_INTERVAL_SECONDS = 5.0


class MemoryIn(BaseModel):
    client_write_id: str = Field(min_length=1)
    content: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    source_client: str = Field(min_length=1)
    event_time: str | None = None
    event_time_precision: str | None = None


class SupersedeIn(BaseModel):
    from_id: str = Field(min_length=1)
    to_id: str = Field(min_length=1)
    kind: str
    reason: str | None = None
    source_client: str = Field(min_length=1)


class SearchIn(BaseModel):
    query: str = Field(min_length=1)
    session_id: str | None = None
    limit: int | None = Field(default=None, ge=1, le=50)


def _embedding_configured(cfg: Config) -> bool:
    """embedding 是否「配置了」（不检查可用性；可用性由 build_provider / 运行期暴露）。"""
    return cfg.embedding_configured


def validate_config(cfg: Config, embedding_provider: EmbeddingProvider | None) -> None:
    """配置错误必须在启动时暴露（§8 硬规则 4），不允许拖到查询时。

    例外（docs/04）：vector 路线启用但 embedding **未配置**时允许降级 bigram，
    不再拒绝启动；配置了却构建失败仍由 ``create_app`` 的 ``build_provider`` 抛错。
    """
    if cfg.rrf_k <= 0:
        raise EmbeddingConfigError(f"RRF_K 必须为正整数（当前 {cfg.rrf_k}）")
    if cfg.route_top_k <= 0:
        raise EmbeddingConfigError(f"ROUTE_TOP_K 必须为正整数（当前 {cfg.route_top_k}）")
    if cfg.final_k <= 0:
        raise EmbeddingConfigError(f"FINAL_K 必须正整数（当前 {cfg.final_k}）")
    if cfg.item_max_chars <= 0 or cfg.total_max_chars <= 0:
        raise EmbeddingConfigError("ITEM_MAX_CHARS / TOTAL_MAX_CHARS 必须为正整数")
    if cfg.item_max_chars > cfg.total_max_chars:
        raise EmbeddingConfigError("ITEM_MAX_CHARS 不得大于 TOTAL_MAX_CHARS")
    if cfg.expand_window < 0:
        raise EmbeddingConfigError("EXPAND_WINDOW 必须 ≥ 0")
    if cfg.embedding_dim_int is None:
        pass  # auto：首次调用推断
    if "vector" in cfg.route_names:
        if embedding_provider is not None:
            if not getattr(embedding_provider, "model_id", ""):
                raise EmbeddingConfigError("注入的 embedding provider 缺少 model_id")
        else:
            logger.warning(
                "vector 路线已启用但 embedding 未配置/不可用；检索将降级 bigram"
                "（配置 EMBEDDING_BASE_URL / EMBEDDING_MODEL 后自动启用向量路）"
            )


# 索引文本升级重建失败后的自动重试：初始尝试 + 3 次重试（30s / 120s / 600s）。
# 全部失败才把 meta 置为 failed；重试期间旧索引原样可用（闸门只在真正重建时置位）。
INDEX_TEXT_REBUILD_RETRY_DELAYS: tuple[float, ...] = (30.0, 120.0, 600.0)


def _run_index_text_rebuild(
    db: Database,
    pipeline: DerivePipeline,
    retrieval: RetrievalService,
    rebuild_gate: RebuildGate,
    stored_version: int | None,
    *,
    retry_delays: tuple[float, ...] | None = None,
    sleep: Callable[[float], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> dict:
    """索引文本版本升级：**后台**跑一次完整派生重建（不阻塞启动），失败自动重试。

    - 走已有 ``pipeline.rebuild_all()``（单写事务；事实层不动）；
    - 成功才在事务内把 meta 版本写成当前值（由 ``rebuild_all`` 负责）；
    - 失败：ERROR 日志 + 重试（默认 30s/120s/600s，最多 3 次）；全部失败才写
      ``index_text_rebuild.status=failed``，旧索引原样保留，由 ``/health.alerts``
      暴露，绝不静默。重试等待期间不持重建闸门，`/search` 可继续读旧索引，
      但会带 ``index_version_stale`` 标记（见 ``RetrievalService``）。
    - 重建期间置 rebuilding 闸门：`/search`、写入与 `/admin/search` 明确 503，
      不返回半张索引。
    - ``retry_delays`` / ``sleep`` / ``cancel_event`` 供测试与进程关闭使用
      （关闭时取消剩余重试，避免 join 卡在 600s 退避里）。

    返回最后一次重建的 meta 详情（成功或失败）。
    """
    delays = (
        INDEX_TEXT_REBUILD_RETRY_DELAYS
        if retry_delays is None
        else tuple(float(d) for d in retry_delays)
    )
    max_attempts = 1 + len(delays)
    sleep_fn = sleep or time.sleep
    started = int(time.time() * 1000)
    last_error: str | None = None

    def _set_status(status: str, *, attempt: int, **extra) -> None:
        payload = {
            "status": status,
            "at": started,
            "from": stored_version,
            "to": INDEX_TEXT_VERSION,
            "attempt": attempt,
            "max_attempts": max_attempts,
        }
        if last_error is not None:
            payload["error"] = last_error
        payload.update(extra)
        try:
            set_meta(
                db,
                INDEX_TEXT_REBUILD_KEY,
                json.dumps(payload, ensure_ascii=False),
            )
        except Exception:
            # 状态写不进 meta 也不能让重建线程本身崩掉；日志 + 返回详情兜底。
            logger.exception("写 index_text_rebuild 状态失败（status=%s）", status)

    def _failed_detail() -> dict:
        detail = {
            "status": "failed",
            "at": started,
            "finished_at": int(time.time() * 1000),
            "from": stored_version,
            "to": INDEX_TEXT_VERSION,
            "attempt": attempt,
            "max_attempts": max_attempts,
            "error": last_error,
        }
        _set_status("failed", attempt=attempt, finished_at=detail["finished_at"])
        return detail

    attempt = 0
    logger.warning(
        "索引文本版本 %s < %s，触发一次后台派生重建（不阻塞启动；"
        "失败自动重试，最多 %d 次尝试）",
        stored_version,
        INDEX_TEXT_VERSION,
        max_attempts,
    )
    try:
        while True:
            attempt += 1
            _set_status("running" if attempt == 1 else "retrying", attempt=attempt)
            try:
                with rebuild_gate.rebuilding():
                    counts = pipeline.rebuild_all()
                    retrieval.invalidate_caches()
                    try:
                        optimize_fts(db, reason="index_text_version")
                    except Exception:
                        # optimize 只是合并 FTS5 段；失败不能让重建整体失败，但必须留日志。
                        logger.exception(
                            "索引文本版本重建后的 FTS5 optimize 失败（状态见 /health）"
                        )
                    detail = {
                        "status": "ok",
                        "at": started,
                        "finished_at": int(time.time() * 1000),
                        "from": stored_version,
                        "to": INDEX_TEXT_VERSION,
                        "attempt": attempt,
                        "max_attempts": max_attempts,
                        "counts": counts,
                    }
                    set_meta(
                        db,
                        INDEX_TEXT_REBUILD_KEY,
                        json.dumps(detail, ensure_ascii=False),
                    )
                logger.info(
                    "索引文本版本重建完成：%s -> %s counts=%s（第 %d/%d 次尝试）",
                    stored_version,
                    INDEX_TEXT_VERSION,
                    counts,
                    attempt,
                    max_attempts,
                )
                return detail
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                logger.error(
                    "索引文本版本重建失败（第 %d/%d 次；旧索引保留，"
                    "/health.index_text_version 可见）：%s",
                    attempt,
                    max_attempts,
                    last_error,
                    exc_info=True,
                )
                if attempt >= max_attempts:
                    detail = _failed_detail()
                    logger.error(
                        "索引文本版本重建在 %d 次尝试后仍失败；已保留 meta failed "
                        "+ /health 告警，等待下次启动或 /admin/rebuild",
                        attempt,
                    )
                    return detail
                delay = delays[attempt - 1]
                _set_status(
                    "retrying",
                    attempt=attempt,
                    next_retry_in_seconds=delay,
                    next_retry_at=int(time.time() * 1000) + int(delay * 1000),
                )
                logger.warning(
                    "索引文本版本重建将在 %.0fs 后重试（下一次为第 %d/%d 次尝试）",
                    delay,
                    attempt + 1,
                    max_attempts,
                )
                if cancel_event is not None:
                    if cancel_event.wait(delay):
                        last_error = (
                            "服务关闭：索引文本版本重建的剩余重试被取消"
                            "（旧索引保留）"
                        )
                        logger.error("%s", last_error)
                        return _failed_detail()
                else:
                    sleep_fn(delay)
    finally:
        # 后台线程的 SQLite 读连接是 thread-local：必须在自己线程里关闭。
        try:
            db.close_thread_read_conn()
        except Exception:
            logger.exception("index-text-rebuild 线程读连接关闭失败")


def create_app(
    db_path: str | None = None,
    *,
    embedding_provider: EmbeddingProvider | None = None,
) -> FastAPI:
    cfg = load_config()
    provider = embedding_provider
    if provider is None and "vector" in cfg.route_names:
        if _embedding_configured(cfg):
            provider = build_provider(cfg)  # 配置/依赖错误在此暴露
        else:
            logger.warning(
                "vector 路线已启用但 embedding 未配置（EMBEDDING_MODEL / "
                "EMBEDDING_BASE_URL 为空）；检索将降级 bigram"
            )
    validate_config(cfg, provider)
    db = Database(
        db_path or cfg.db_path,
        wal_autocheckpoint_pages=cfg.wal_autocheckpoint_pages,
    )
    # 先共用同一个向量内存矩阵，再构造流水线（构造函数不碰库）：这样 init_db 的
    # 迁移后强制重建能用上配置好的 embedding provider，派生 processor 与检索服务
    # 也共享同一个 VectorIndex（写事务提交后的增量刷新才有效）。
    vector_index = VectorIndex()
    pipeline = DerivePipeline(
        db,
        stub_success=cfg.derive_stub_success,
        embedding_provider=provider,
        embedding_dim=cfg.embedding_dim_int,
        vector_index=vector_index,
        batch_size=cfg.derive_batch_size,
    )
    init_report = init_db(db, pipeline=pipeline)
    if init_report.rebuild_attempted:
        if init_report.rebuild_ok:
            logger.info(
                "migration rebuild done: counts=%s embedding_skipped=%s",
                init_report.rebuild_counts,
                init_report.embedding_skipped,
            )
        else:
            logger.error(
                "migration rebuild FAILED（/health.migration_needs_rebuild=true）: %s",
                init_report.rebuild_error,
            )
    # 索引文本版本（docs/02 v9）：init_db 的迁移重建若已跑过，版本已由 rebuild_all
    # 写成当前值；否则在 lifespan 里起一个后台线程补一次，启动本身不等待。
    index_text_stored_version = get_index_text_version(db)
    index_text_needs_rebuild = index_text_rebuild_needed(db)
    if index_text_needs_rebuild:
        logger.warning(
            "检测到索引文本版本 %s < %s（已有记录）：将在后台触发一次派生重建"
            "（事实层不动；成功前 /health.index_text_version.up_to_date=false）",
            index_text_stored_version,
            INDEX_TEXT_VERSION,
        )
    # retrieval 必须在 init_db 之后构造（resolve_routes 要读 meta）
    retrieval = RetrievalService(db, cfg, provider, index=vector_index)
    backup_scheduler = BackupScheduler(
        db,
        interval_seconds=cfg.backup_interval_seconds,
        keep=cfg.backup_keep,
    )
    # 派生循环、自动备份、WAL 检查点、FTS5 optimize 共用同一个后台线程：
    # **任一开启**都要起线程，否则 derive=0 + backup=0 的合法部署（bigram-only
    # 无备份）会让检查点与 optimize 一次都不跑且无任何告警。
    # derive_loop_enabled 已由 config.load_config 按「vector + embedding 已配置」
    # 默认开启（显式 DSH_DERIVE_LOOP_ENABLED=0 才关），否则写入后 embedding
    # 永久 pending、默认向量路线永久降级（联调实测 P0）。
    derive_loop_enabled = cfg.derive_loop_enabled
    loop_needed = (
        derive_loop_enabled
        or cfg.backup_interval_seconds > 0
        or cfg.wal_checkpoint_interval_seconds > 0
        or cfg.fts_optimize_interval_seconds > 0
    )
    # WAL 检查点与 FTS5 optimize 的后台策略（复用下面的 derive-backup-loop 线程）。
    maintenance = Maintenance(
        db,
        checkpoint_interval_seconds=cfg.wal_checkpoint_interval_seconds,
        checkpoint_idle_seconds=cfg.wal_checkpoint_idle_seconds,
        fts_optimize_interval_seconds=cfg.fts_optimize_interval_seconds,
        loop_enabled=loop_needed,
    )
    # 重建期闸门 + /health 快照缓存都在进程内：重建只有一个，/health 只读缓存。
    rebuild_gate = RebuildGate()

    def _compute_health() -> dict:
        """/health 的全部昂贵统计：只在后台刷新线程（或启动 prime）里执行。"""
        report = health_report(
            db, pipeline, backup_scheduler, retrieval, maintenance
        )
        if cfg.canary_enabled:
            # 金丝雀查询不计入检索统计（直接走 bigram_route）
            try:
                report["retrieval"]["canary"] = retrieval.run_canary()
            except Exception:  # 金丝雀失败不影响 /health 其余字段
                logger.exception("canary failed")
                report["retrieval"]["canary"] = {
                    "status": "error",
                    "alert": False,
                }
        return report

    health_cache = HealthCache(
        _compute_health,
        interval_seconds=cfg.health_refresh_seconds,
        rebuilding=rebuild_gate,
        on_thread_stop=db.close_thread_read_conn,
    )

    stop_event = threading.Event()
    loop_thread: threading.Thread | None = None
    index_text_thread: threading.Thread | None = None

    def _loop() -> None:
        try:
            while not stop_event.wait(DERIVE_LOOP_INTERVAL_SECONDS):
                if derive_loop_enabled:
                    try:
                        pipeline.tick()
                    except Exception:  # 派生失败不拖垮备份检查
                        logger.exception("derive loop tick failed")
                try:
                    backup_scheduler.maybe_backup()
                except Exception as exc:  # 不吞异常：完整堆栈已在调度器内记日志，状态进 /health
                    logger.error("scheduled backup check failed: %s", exc)
                # 维护动作互不影响，也不拖垮派生/备份：全部只记日志，状态进 /health。
                try:
                    maintenance.maybe_checkpoint_truncate()
                except Exception:
                    logger.exception("background wal checkpoint maintenance failed")
                try:
                    maintenance.maybe_optimize_fts()
                except Exception:
                    logger.exception("background fts optimize maintenance failed")
        finally:
            # 后台线程的 SQLite 读连接是 thread-local：必须在自己线程里关闭，
            # 否则每次重起线程都会泄漏一个只读连接（与 health-cache 同口径）。
            try:
                db.close_thread_read_conn()
            except Exception:
                logger.exception("background loop read connection close failed")

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        nonlocal loop_thread, index_text_thread
        # 先 prime 一份快照（启动即可回答 /health），再起周期刷新线程。
        health_cache.start()
        # 索引文本升级：后台重建，不阻塞启动（重建期闸门让读写明确 503，不返回半张索引）。
        if index_text_needs_rebuild:
            index_text_thread = threading.Thread(
                target=_run_index_text_rebuild,
                args=(
                    db,
                    pipeline,
                    retrieval,
                    rebuild_gate,
                    index_text_stored_version,
                ),
                kwargs={"cancel_event": stop_event},
                name="index-text-rebuild",
                daemon=True,
            )
            index_text_thread.start()
            app.state.index_text_rebuild_thread = index_text_thread
        if loop_needed:
            loop_thread = threading.Thread(
                target=_loop, name="derive-backup-loop", daemon=True
            )
            loop_thread.start()
            app.state.derive_loop_thread = loop_thread
            if derive_loop_enabled:
                # 联调验收要求这条日志明确可查（routes=vector）。
                logger.info(
                    "deriveloop started (routes=%s)", ",".join(cfg.route_names)
                )
            logger.info(
                "background loop started (interval=%ss, derive=%s, backup_interval=%ss, "
                "wal_checkpoint_interval=%ss, fts_optimize_interval=%ss)",
                DERIVE_LOOP_INTERVAL_SECONDS,
                derive_loop_enabled,
                cfg.backup_interval_seconds,
                cfg.wal_checkpoint_interval_seconds,
                cfg.fts_optimize_interval_seconds,
            )
        else:
            # 全关也必须留痕：否则「检查点/optimize 从未运行」只能靠猜。
            logger.info(
                "维护线程未启动：derive=%s，backup_interval=%ss，"
                "wal_checkpoint_interval=%ss，fts_optimize_interval=%ss；"
                "checkpoint/optimize 均关闭",
                derive_loop_enabled,
                cfg.backup_interval_seconds,
                cfg.wal_checkpoint_interval_seconds,
                cfg.fts_optimize_interval_seconds,
            )
        yield
        health_cache.stop()
        stop_event.set()
        if loop_thread is not None:
            loop_thread.join(timeout=30)
        if index_text_thread is not None:
            # 重建写事务完成/失败后线程才退出；先等它结束再关库，避免半途断连接。
            index_text_thread.join(timeout=60)
        db.close()

    app = FastAPI(title="factlayer", lifespan=lifespan)
    app.state.db = db
    app.state.pipeline = pipeline
    app.state.config = cfg
    app.state.retrieval = retrieval
    app.state.backup_scheduler = backup_scheduler
    app.state.maintenance = maintenance
    app.state.rebuild_gate = rebuild_gate
    app.state.health_cache = health_cache
    app.state.derive_loop_enabled = derive_loop_enabled
    app.state.derive_loop_thread = None
    app.state.index_text_needs_rebuild = index_text_needs_rebuild
    app.state.index_text_stored_version = index_text_stored_version
    app.state.index_text_rebuild_thread = None

    def _admin_allowed(request: Request) -> bool:
        """ADMIN_TOKEN 非空 → 校验 token；为空 → 只接受回环（含 TestClient）。"""
        if cfg.admin_token:
            supplied = request.headers.get("x-admin-token", "")
            if not supplied:
                auth = request.headers.get("authorization", "")
                if auth.lower().startswith("bearer "):
                    supplied = auth[7:].strip()
            return supplied == cfg.admin_token
        host = request.client.host if request.client is not None else ""
        return host in ("127.0.0.1", "::1", "localhost", "testclient")

    def _require_admin(request: Request) -> None:
        if not _admin_allowed(request):
            raise HTTPException(status_code=403, detail="admin 端点需要 ADMIN_TOKEN 或回环访问")

    @app.exception_handler(sqlite3.Error)
    def _sqlite_error_handler(_request: Request, exc: sqlite3.Error) -> JSONResponse:
        """任何未捕获的 SQLite 错误：原始信息只进服务端日志，响应给友好消息。"""
        logger.error(
            "unhandled sqlite error", exc_info=(type(exc), exc, exc.__traceback__)
        )
        return JSONResponse(
            status_code=500, content={"detail": "内部存储错误，服务端已记录日志"}
        )

    @app.exception_handler(RebuildingInProgress)
    def _rebuilding_handler(
        _request: Request, _exc: RebuildingInProgress
    ) -> JSONResponse:
        """重建期：明确 503，绝不用空数组/半张索引冒充正常结果。"""
        return JSONResponse(
            status_code=503,
            content={"error": REBUILDING_ERROR, "message": REBUILDING_MESSAGE},
        )

    @app.get("/spaces")
    def get_spaces() -> list[dict]:
        """provider discover 的命名空间列表（O(1)，只读配置，不查库）。

        固定单空间：id 取 ``FACTLAYER_SPACE_ID``（默认 ``factlayer-default``）。
        discover 的 externalId 就是这个值，remember 的 session_id 也取同一个值
        （由适配器从空间 connection 里读），因此两台机器天然一致。
        """
        return [{"id": cfg.factlayer_space_id, "name": "factlayer"}]

    @app.post("/memories")
    def post_memory(payload: MemoryIn) -> dict:
        # 重建期不接收写入：新记忆若落在「重建读快照」之后，会缺派生行（搜不到）。
        rebuild_gate.raise_if_rebuilding()
        return create_memory(
            db,
            client_write_id=payload.client_write_id,
            content=payload.content,
            session_id=payload.session_id,
            source_client=payload.source_client,
            event_time=payload.event_time,
            event_time_precision=payload.event_time_precision,
        )

    @app.get("/memories/{memory_id}")
    def get_memory(memory_id: str) -> dict:
        record = read_path.get_memory_full(db, memory_id)
        if record is None:
            raise HTTPException(status_code=404, detail="memory not found")
        return record  # 永远返回全文，不截断

    @app.get("/memories")
    def list_memories(
        session_id: str = Query(min_length=1),
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ) -> dict:
        rebuild_gate.raise_if_rebuilding()
        records = read_path.list_memories_by_session(
            db, session_id, limit=limit, offset=offset, truncate=True
        )
        return {"session_id": session_id, "count": len(records), "items": records}

    @app.post("/supersedes")
    def post_supersede(payload: SupersedeIn) -> dict:
        # 重建期同样关闭写边：supersede 边若落在「重建读快照」之后，派生层会
        # 少算一次作废，产生「边已写、检索却按旧图作答」的派生不一致。与
        # /memories 保持同一语义（明确 503，客户端可重试），不选「先写边、
        # 依赖下一次 derive 补齐」——那会留下一个静默的不一致窗口
        # （第三层审查 #2，方案 a）。
        rebuild_gate.raise_if_rebuilding()
        return record_supersede(
            db,
            from_id=payload.from_id,
            to_id=payload.to_id,
            kind=payload.kind,
            reason=payload.reason,
            source_client=payload.source_client,
        )

    @app.get("/supersedes/state")
    def supersede_state(from_id: str, to_id: str) -> dict:
        edge = read_path.latest_edge_for_pair(db, from_id, to_id)
        if edge is None:
            raise HTTPException(status_code=404, detail="no edge for this pair")
        return edge

    @app.get("/memories/{memory_id}/voided")  # noqa: 需在 /memories/{id} 之后定义亦可
    def get_voided(memory_id: str) -> dict:
        if read_path.get_memory_full(db, memory_id) is None:
            raise HTTPException(status_code=404, detail="memory not found")
        return {"id": memory_id, "superseded": read_path.is_voided(db, memory_id)}

    @app.post("/search")
    def post_search(payload: SearchIn) -> dict:
        # 重建期：明确 503（docs/06 §7.1），不返回部分结果。
        # 注意 /search **不按 session_id 过滤**：session_id 只用于写入分组与会话展开，
        # 契约里也没有「按会话检索」的概念（docs/06 §5）。这里保留字段仅作回显。
        rebuild_gate.raise_if_rebuilding()
        try:
            return retrieval.search(
                payload.query,
                session_id=payload.session_id,
                limit=payload.limit,
            )
        except EmbeddingModelMismatch as exc:
            detail = exc.message
            if "/admin/rebuild" not in detail:
                detail += " 请执行 POST /admin/rebuild 重建向量后重试。"
            raise HTTPException(status_code=409, detail=detail) from None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @app.get("/health")
    def get_health() -> dict:
        """只读后台快照（O(1)）：请求里不跑聚合查询、不加锁、不触发派生。"""
        snapshot = health_cache.snapshot()
        if snapshot is None:
            return {"status": "starting"}
        return snapshot

    @app.post("/derive/tick")
    def derive_tick() -> dict:
        # 重建期不跑派生循环：派生写的是同一张派生层，和全量重建并发只会
        # 白跑一遍（重建事务结束会覆盖），还可能让 reader 看到半张索引。
        rebuild_gate.raise_if_rebuilding()
        processed = pipeline.tick()
        # 向量写入已在写事务提交后增量刷新 retrieval.index；这里不再整表失效。
        return processed

    @app.post("/admin/rebuild")
    def admin_rebuild(request: Request) -> dict:
        _require_admin(request)
        # 已有重建在跑时不再排队：明确 503（客户端可重试），避免两个全量重建
        # 并发清空/重填同一批派生表，白耗一次完整重建。
        rebuild_gate.raise_if_rebuilding()
        # 触发即置 rebuilding=True（线程安全标志），完成/失败都清除；
        # 期间读端点与写端点（含 /admin/search 与 /supersedes）返回 503，
        # 绝不让读者看到半张索引、也不让写边落在读快照之后。
        with rebuild_gate.rebuilding():
            counts = pipeline.rebuild_all()
            retrieval.invalidate_caches()
            # 重建后合并 FTS5 段；optimize 失败只记日志/写 /health，不让重建失败。
            try:
                fts_status = optimize_fts(db, reason="admin_rebuild")
                # 同步后台维护缓存：否则下一 tick 会按旧时间再 optimize 一遍。
                maintenance.note_optimize_attempt()
            except Exception:
                logger.exception(
                    "admin rebuild 后的 FTS5 optimize 失败（状态见 /health.fts_optimize）"
                )
                fts_status = {"ok": False, "errors": ["optimize failed"]}
        return {"ok": True, "counts": counts, "fts_optimize": fts_status}

    @app.post("/admin/checkpoint")
    def admin_checkpoint(
        request: Request,
        mode: str = Query("PASSIVE"),
    ) -> dict:
        """手动 WAL 检查点（默认 PASSIVE，不阻塞读者）。

        返回 ``{busy, log_pages, checkpointed_pages}``；busy=1 表示 TRUNCATE 被
        读者/写者挡住（状态会进 ``/health.sqlite.last_checkpoint_error``），不是 500。
        """
        _require_admin(request)
        normalized = mode.upper()
        if normalized not in CHECKPOINT_MODES:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"未知 checkpoint mode={mode!r}；"
                    f"可用：{', '.join(CHECKPOINT_MODES)}"
                ),
            )
        return db.wal_checkpoint(normalized)

    @app.get("/admin/search")
    def admin_search(
        request: Request,
        query: str = Query(min_length=1, max_length=2000),
        include_superseded: int = Query(0),
        limit: int | None = Query(None, ge=1, le=50),
    ) -> dict:
        """运维/评测端点：模型路径不可达的 include_superseded 只在这里可用。"""
        _require_admin(request)
        # 重建期同样 503：admin 检索读的是同一张索引，不能拿半张结果做运维/评测
        # 结论（第三层审查 #2）。
        rebuild_gate.raise_if_rebuilding()
        try:
            return retrieval.search(
                query,
                limit=limit,
                include_superseded=bool(include_superseded),
            )
        except EmbeddingModelMismatch as exc:
            raise HTTPException(status_code=409, detail=exc.message) from None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @app.post("/admin/canary")
    def admin_canary(request: Request) -> dict:
        _require_admin(request)
        return retrieval.run_canary()

    @app.post("/admin/backup")
    def admin_backup(request: Request) -> dict:
        _require_admin(request)
        try:
            dest = backup_scheduler.run_now()
        except Exception:
            # 原始错误已由调度器写日志；HTTP 响应不泄露 SQLite/文件系统细节。
            raise HTTPException(
                status_code=500, detail="备份失败，服务端已记录日志"
            ) from None
        return {"ok": True, "path": dest}

    return app


def main() -> None:
    import uvicorn

    cfg = load_config()
    # 单 worker：写路径依赖进程内单写锁，禁止多 worker。
    # 用 factory 字符串启动，避免模块级 app 的 import 副作用。
    uvicorn.run(
        "app.main:create_app",
        factory=True,
        host="127.0.0.1",
        port=cfg.server_port,
        workers=1,
    )


if __name__ == "__main__":
    main()
