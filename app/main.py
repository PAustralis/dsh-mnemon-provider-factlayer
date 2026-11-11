"""FastAPI 装配（工厂模式，import 无副作用）。

端点：POST /memories、GET /memories/{id}、GET /memories（按 session）、
POST /supersedes、GET /health、POST /search（检索层），以及运维辅助
POST /derive/tick、POST /admin/rebuild、POST /admin/backup、GET /admin/search、
POST /admin/canary。部署固定单 worker：

    python -m app.main
    python -m uvicorn app.main:create_app --factory --workers 1

admin 端点由 ``ADMIN_TOKEN`` 保护：非空时要求 ``X-Admin-Token`` 或
``Authorization: Bearer <token>``；为空时只接受回环请求（含 TestClient）。
``include_superseded`` 在模型路径上不可达，只由 ``GET /admin/search`` 提供。

模块级不创建 ``app``：``import app.main`` 不会建库、不会产生 facts.db。
配置错误（如启用 vector 路线但 EMBEDDING_MODEL 为空）在 ``create_app`` 时即抛错。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .backup import BackupScheduler
from .config import Config, load_config
from .db import Database
from .derive import DerivePipeline
from .embeddings import (
    EmbeddingConfigError,
    EmbeddingModelMismatch,
    EmbeddingProvider,
    build_provider,
)
from .health import health_report
from .retrieval import RetrievalService
from .schema import init_db
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


def validate_config(cfg: Config, embedding_provider: EmbeddingProvider | None) -> None:
    """配置错误必须在启动时暴露（§8 硬规则 4），不允许拖到查询时。"""
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
        elif not cfg.embedding_model.strip():
            raise EmbeddingConfigError(
                "启用 vector 路线（RETRIEVAL_ROUTES 含 vector）但 EMBEDDING_MODEL 为空；"
                "请设置 EMBEDDING_MODEL，或把 vector 从 RETRIEVAL_ROUTES 移除。"
            )
        else:
            build_provider(cfg)  # 校验后端 / 依赖（local-onnx import 失败即报错）


def create_app(
    db_path: str | None = None,
    *,
    embedding_provider: EmbeddingProvider | None = None,
) -> FastAPI:
    cfg = load_config()
    provider = embedding_provider
    if provider is None and "vector" in cfg.route_names:
        provider = build_provider(cfg)  # 配置/依赖错误在此暴露
    validate_config(cfg, provider)
    db = Database(db_path or cfg.db_path)
    init_db(db)
    retrieval = RetrievalService(db, cfg, provider)
    pipeline = DerivePipeline(
        db,
        stub_success=cfg.derive_stub_success,
        embedding_provider=provider,
        embedding_dim=cfg.embedding_dim_int,
        # 写事务提交后增量刷新检索层的内存向量矩阵（与 retrieval 共用同一实例）
        vector_index=retrieval.index,
    )
    backup_scheduler = BackupScheduler(
        db,
        interval_seconds=cfg.backup_interval_seconds,
        keep=cfg.backup_keep,
    )

    stop_event = threading.Event()
    loop_thread: threading.Thread | None = None
    # 派生循环与自动备份共用同一个后台线程：任一开启即启动。
    loop_needed = cfg.derive_loop_enabled or cfg.backup_interval_seconds > 0

    def _loop() -> None:
        while not stop_event.wait(DERIVE_LOOP_INTERVAL_SECONDS):
            if cfg.derive_loop_enabled:
                try:
                    pipeline.tick()
                except Exception:  # 派生失败不拖垮备份检查
                    logger.exception("derive loop tick failed")
            try:
                backup_scheduler.maybe_backup()
            except Exception as exc:  # 不吞异常：完整堆栈已在调度器内记日志，状态进 /health
                logger.error("scheduled backup check failed: %s", exc)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        nonlocal loop_thread
        if loop_needed:
            loop_thread = threading.Thread(
                target=_loop, name="derive-backup-loop", daemon=True
            )
            loop_thread.start()
            logger.info(
                "background loop started (interval=%ss, derive=%s, backup_interval=%ss)",
                DERIVE_LOOP_INTERVAL_SECONDS,
                cfg.derive_loop_enabled,
                cfg.backup_interval_seconds,
            )
        yield
        stop_event.set()
        if loop_thread is not None:
            loop_thread.join(timeout=30)
        db.close()

    app = FastAPI(title="factlayer", lifespan=lifespan)
    app.state.db = db
    app.state.pipeline = pipeline
    app.state.config = cfg
    app.state.retrieval = retrieval
    app.state.backup_scheduler = backup_scheduler

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

    @app.post("/memories")
    def post_memory(payload: MemoryIn) -> dict:
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
        records = read_path.list_memories_by_session(
            db, session_id, limit=limit, offset=offset, truncate=True
        )
        return {"session_id": session_id, "count": len(records), "items": records}

    @app.post("/supersedes")
    def post_supersede(payload: SupersedeIn) -> dict:
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
        report = health_report(db, pipeline, backup_scheduler, retrieval)
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

    @app.post("/derive/tick")
    def derive_tick() -> dict:
        processed = pipeline.tick()
        # 向量写入已在写事务提交后增量刷新 retrieval.index；这里不再整表失效。
        return processed

    @app.post("/admin/rebuild")
    def admin_rebuild(request: Request) -> dict:
        _require_admin(request)
        counts = pipeline.rebuild_all()
        retrieval.invalidate_caches()
        return {"ok": True, "counts": counts}

    @app.get("/admin/search")
    def admin_search(
        request: Request,
        query: str = Query(min_length=1, max_length=2000),
        include_superseded: int = Query(0),
        limit: int | None = Query(None, ge=1, le=50),
    ) -> dict:
        """运维/评测端点：模型路径不可达的 include_superseded 只在这里可用。"""
        _require_admin(request)
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
