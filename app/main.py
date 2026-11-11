"""FastAPI 装配（工厂模式，import 无副作用）。

端点：POST /memories、GET /memories/{id}、GET /memories（按 session）、
POST /supersedes、GET /health，以及本层运维辅助 POST /derive/tick、
POST /admin/rebuild、POST /admin/backup。部署固定单 worker：

    python -m app.main
    python -m uvicorn app.main:create_app --factory --workers 1

模块级不再创建 ``app``：``import app.main`` 不会建库、不会产生 facts.db。
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
from .config import load_config
from .db import Database
from .derive import DerivePipeline
from .health import health_report
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


def create_app(db_path: str | None = None) -> FastAPI:
    cfg = load_config()
    db = Database(db_path or cfg.db_path)
    init_db(db)
    pipeline = DerivePipeline(db, stub_success=cfg.derive_stub_success)
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
    app.state.backup_scheduler = backup_scheduler

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

    @app.get("/health")
    def get_health() -> dict:
        return health_report(db, pipeline, backup_scheduler)

    @app.post("/derive/tick")
    def derive_tick() -> dict:
        return pipeline.tick()

    @app.post("/admin/rebuild")
    def admin_rebuild() -> dict:
        pipeline.rebuild_all()
        return {"ok": True}

    @app.post("/admin/backup")
    def admin_backup() -> dict:
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
