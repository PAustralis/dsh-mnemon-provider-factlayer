"""读取路径：按 id 取全文（永不截断）、按会话取记录（读取输出处截断）、作废状态判定。"""

from __future__ import annotations

from .db import Database

TRUNCATE_LIMIT = 1200


def truncate_for_output(content: str, memory_id: str) -> str:
    """只在读取输出处截断，标注「[已截断，原文 N 字，id=…]」；入库永远是全文。"""
    if len(content) <= TRUNCATE_LIMIT:
        return content
    marker = f"[已截断，原文 {len(content)} 字，id={memory_id}]"
    return content[:TRUNCATE_LIMIT] + marker


def _row_to_dict(row) -> dict:
    return {
        "id": row["id"],
        "client_write_id": row["client_write_id"],
        "content": row["content"],
        "recorded_at": row["recorded_at"],
        "session_id": row["session_id"],
        "seq": row["seq"],
        "event_time": row["event_time"],
        "event_time_precision": row["event_time_precision"],
        "source_client": row["source_client"],
    }


def get_memory_full(db: Database, memory_id: str) -> dict | None:
    """按 id 取单条全文（不截断）。"""
    row = db.read_conn.execute(
        "SELECT * FROM memories WHERE id=?", (memory_id,)
    ).fetchone()
    if row is None:
        return None
    record = _row_to_dict(row)
    record["superseded"] = is_voided(db, memory_id)
    return record


def list_memories_by_session(
    db: Database,
    session_id: str,
    *,
    limit: int = 50,
    offset: int = 0,
    truncate: bool = True,
) -> list[dict]:
    """按会话取记录，会话内以 seq 排序。输出处截断超长 content。"""
    rows = db.read_conn.execute(
        "SELECT * FROM memories WHERE session_id=? ORDER BY seq LIMIT ? OFFSET ?",
        (session_id, limit, offset),
    ).fetchall()
    records = [_row_to_dict(r) for r in rows]
    if truncate:
        for record in records:
            record["content_truncated"] = len(record["content"]) > TRUNCATE_LIMIT
            record["content"] = truncate_for_output(record["content"], record["id"])
    return records


def latest_edge_for_pair(db: Database, from_id: str, to_id: str) -> dict | None:
    """对 (from_id,to_id) 取 recorded_at 最新一条边。"""
    row = db.read_conn.execute(
        "SELECT * FROM supersedes WHERE from_id=? AND to_id=? "
        "ORDER BY recorded_at DESC LIMIT 1",
        (from_id, to_id),
    ).fetchone()
    if row is None:
        return None
    return {
        "from_id": row["from_id"],
        "to_id": row["to_id"],
        "kind": row["kind"],
        "reason": row["reason"],
        "recorded_at": row["recorded_at"],
        "source_client": row["source_client"],
    }


def is_voided(db: Database, to_id: str) -> bool:
    """某条当前是否作废：对每个 (from_id,to_id) 取最新边，
    任一最新边为 supersede → 作废；全部为 revoke 或无边 → 未作废。"""
    rows = db.read_conn.execute(
        "SELECT from_id, kind FROM supersedes WHERE to_id=? "
        "ORDER BY recorded_at DESC",
        (to_id,),
    ).fetchall()
    latest_by_pair: dict[str, str] = {}
    for row in rows:  # 已按 recorded_at 降序，首个出现的即该 pair 最新边
        if row["from_id"] not in latest_by_pair:
            latest_by_pair[row["from_id"]] = row["kind"]
    return any(kind == "supersede" for kind in latest_by_pair.values())
