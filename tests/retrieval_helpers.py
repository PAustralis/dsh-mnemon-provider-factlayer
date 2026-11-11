"""检索层测试的共享构造助手（非测试文件，pytest 不会收集）。"""

from __future__ import annotations

import itertools
from dataclasses import replace

from app.config import load_config
from app.db import Database
from app.derive import DerivePipeline
from app.retrieval import RetrievalService
from app.write_path import create_memory

_counter = itertools.count()


def insert_memory(
    db: Database,
    memory_id: str,
    content: str,
    *,
    session_id: str = "s1",
    seq: int = 0,
    recorded_at: int = 1_700_000_000_000,
) -> str:
    """直接 INSERT 一条事实行（append-only 表不能 UPDATE，故测试用直插控制时间/seq）。"""
    with db.write_txn() as conn:
        conn.execute(
            "INSERT INTO memories (id, client_write_id, content, recorded_at,"
            " session_id, seq, source_client) VALUES (?,?,?,?,?,?,?)",
            (memory_id, f"cw-{memory_id}", content, recorded_at, session_id, seq, "t"),
        )
    return memory_id


def add_memory(
    db: Database,
    content: str,
    *,
    session_id: str | None = None,
    recorded_at: int | None = None,
) -> str:
    """走写入路径加一条记录；给了 recorded_at 时改用直插（写路径只取服务端时间）。"""
    if recorded_at is not None:
        memory_id = f"m{next(_counter):06d}"
        return insert_memory(
            db,
            memory_id,
            content,
            session_id=session_id or f"s{next(_counter)}",
            seq=next(_counter),
            recorded_at=recorded_at,
        )
    session = session_id or f"s{next(_counter)}"
    receipt = create_memory(
        db,
        client_write_id=f"cw-{session}-{next(_counter)}",
        content=content,
        session_id=session,
        source_client="t",
    )
    return receipt["id"]


def add_session(db: Database, contents: list[str], *, session_id: str) -> list[str]:
    """同一会话内按 seq 连续写入一组记录，返回 id 列表。"""
    ids: list[str] = []
    for seq, content in enumerate(contents):
        memory_id = f"m{next(_counter):06d}"
        insert_memory(
            db,
            memory_id,
            content,
            session_id=session_id,
            seq=seq,
            recorded_at=1_700_000_000_000 + seq,
        )
        ids.append(memory_id)
    return ids


def add_edge(
    db: Database,
    from_id: str,
    to_id: str,
    *,
    kind: str = "supersede",
    recorded_at: int = 1_700_000_000_000,
) -> None:
    with db.write_txn() as conn:
        conn.execute(
            "INSERT INTO supersedes "
            "(from_id, to_id, kind, reason, recorded_at, source_client) "
            "VALUES (?,?,?,?,?,?)",
            (from_id, to_id, kind, None, recorded_at, "t"),
        )


def build_derived(db: Database, *, embedding_provider=None, embedding_dim=None) -> DerivePipeline:
    """跑水位循环直到 fts（以及给定 provider 时的 embedding）补齐。"""
    pipeline = DerivePipeline(
        db,
        stub_success=True,
        embedding_provider=embedding_provider,
        embedding_dim=embedding_dim,
    )
    for _ in range(50):
        if pipeline.pending_count("fts") == 0 and (
            embedding_provider is None or pipeline.pending_count("embedding") == 0
        ):
            break
        pipeline.tick()
    assert pipeline.pending_count("fts") == 0
    if embedding_provider is not None:
        assert pipeline.pending_count("embedding") == 0
    return pipeline


def make_service(db: Database, *, routes=("bigram",), provider=None, **overrides) -> RetrievalService:
    cfg = replace(load_config(), retrieval_routes=tuple(routes), **overrides)
    return RetrievalService(db, cfg, provider)
