"""FastAPI 装配。

端点：POST /memories、GET /memories/{id}、GET /memories（按 session）、
POST /supersedes、GET /health，以及本层运维辅助 POST /derive/tick、
POST /admin/rebuild、POST /admin/backup。部署固定单 worker
（``python -m app.main`` 或 ``uvicorn app.main:app --workers 1``）。
"""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from .backup import run_backup
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

    stop_event = threading.Event()
    loop_thread: threading.Thread | None = None

    def _loop() -> None:
        while not stop_event.wait(DERIVE_LOOP_INTERVAL_SECONDS):
            try:
                pipeline.tick()
            except Exception:  # 后台循环绝不带崩服务
                logger.exception("derive loop tick failed")

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        nonlocal loop_thread
        if cfg.derive_loop_enabled:
            loop_thread = threading.Thread(
                target=_loop, name="derive-loop", daemon=True
            )
            loop_thread.start()
            logger.info("derive loop thread started (interval=%ss)", DERIVE_LOOP_INTERVAL_SECONDS)
        yield
        stop_event.set()
        db.close()

    app = FastAPI(title="factlayer", lifespan=lifespan)
    app.state.db = db
    app.state.pipeline = pipeline
    app.state.config = cfg

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
        return health_report(db, pipeline)

    @app.post("/derive/tick")
    def derive_tick() -> dict:
        return pipeline.tick()

    @app.post("/admin/rebuild")
    def admin_rebuild() -> dict:
        pipeline.rebuild_all()
        return {"ok": True}

    @app.post("/admin/backup")
    def admin_backup() -> dict:
        dest = run_backup(db, keep=cfg.backup_keep)
        return {"ok": True, "path": dest}

    return app


app = create_app()


def main() -> None:
    import uvicorn

    cfg = load_config()
    # 单 worker：写路径依赖进程内单写锁，禁止多 worker
    uvicorn.run(app, host="127.0.0.1", port=cfg.server_port, workers=1)


if __name__ == "__main__":
    main()
