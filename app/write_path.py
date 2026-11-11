"""写入路径：零判断、只有机械动作。服务端分配 id/seq，client_write_id 幂等，不一致 409。"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
import uuid

from fastapi import HTTPException

from . import textnorm
from . import voided as voided_mod
from .db import Database

logger = logging.getLogger("factlayer.write_path")

# 幂等比较字段：同 client_write_id 重试时，这些字段必须完全一致
IDEMPOTENCY_FIELDS = (
    "content",
    "session_id",
    "source_client",
    "event_time",
    "event_time_precision",
)

_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# 写入层显式上限（v8 §3.1）：超限直接拒绝，而不是让派生层在超长文本上失败。
CONTENT_MAX_CHARS = 100_000


def _valid_date(text: str) -> bool:
    try:
        time.strptime(text, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def validate_event_time(event_time: str | None, precision: str | None) -> None:
    """应用层校验（表 CHECK 之外再给友好错误）：
    - 与 event_time_precision 同空同有
    - precision ∈ {year, month, day, instant}
    - year 存 '2026'；month 存 '2026-11'；day/instant 存 '2026-11-11'
    """
    if (event_time is None) != (precision is None):
        raise HTTPException(
            status_code=422,
            detail="event_time 与 event_time_precision 必须同空同有",
        )
    if event_time is None:
        return
    if precision not in ("year", "month", "day", "instant"):
        raise HTTPException(
            status_code=422, detail=f"event_time_precision 非法: {precision!r}"
        )
    if precision == "year":
        ok = bool(re.match(r"^\d{4}$", event_time))
    elif precision == "month":
        ok = bool(_MONTH_RE.match(event_time))
    else:  # day / instant 均存完整日期
        ok = bool(_DAY_RE.match(event_time)) and _valid_date(event_time)
    if not ok:
        raise HTTPException(
            status_code=422,
            detail=f"event_time={event_time!r} 与 precision={precision!r} 格式不匹配"
            "（year: '2026'；month: '2026-11'；day/instant: '2026-11-11'）",
        )


def now_ms() -> int:
    return int(time.time() * 1000)


def create_memory(
    db: Database,
    *,
    client_write_id: str,
    content: str,
    session_id: str,
    source_client: str,
    event_time: str | None = None,
    event_time_precision: str | None = None,
) -> dict:
    """写入一条记忆。幂等：同 client_write_id 且比较字段一致 → 返回已有回执（duplicated:true）；
    不一致 → 409，绝不静默。"""
    validate_event_time(event_time, event_time_precision)
    if len(content) > CONTENT_MAX_CHARS:
        raise HTTPException(
            status_code=413,
            detail=f"content 超长（{len(content)} > {CONTENT_MAX_CHARS} 字符），已拒绝写入",
        )

    with db.write_txn() as conn:
        row = conn.execute(
            "SELECT id, seq, recorded_at, content, session_id, source_client, "
            "event_time, event_time_precision FROM memories WHERE client_write_id=?",
            (client_write_id,),
        ).fetchone()
        if row is not None:
            provided = {
                "content": content,
                "session_id": session_id,
                "source_client": source_client,
                "event_time": event_time,
                "event_time_precision": event_time_precision,
            }
            for field in IDEMPOTENCY_FIELDS:
                if row[field] != provided[field]:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"client_write_id={client_write_id} 已存在且 {field} 不一致："
                            f"库内 {row[field]!r} vs 本次 {provided[field]!r}"
                        ),
                    )
            return {
                "id": row["id"],
                "seq": row["seq"],
                "recorded_at": row["recorded_at"],
                "client_write_id": client_write_id,
                "duplicated": True,
            }

        # seq 服务端在写事务（BEGIN IMMEDIATE）内分配，会话内严格连续
        seq = conn.execute(
            "SELECT COALESCE(MAX(seq),-1)+1 FROM memories WHERE session_id=?",
            (session_id,),
        ).fetchone()[0]
        memory_id = uuid.uuid4().hex
        recorded_at = now_ms()
        conn.execute(
            "INSERT INTO memories "
            "(id, client_write_id, content, recorded_at, session_id, seq, "
            " event_time, event_time_precision, source_client) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                memory_id,
                client_write_id,
                content,
                recorded_at,
                session_id,
                seq,
                event_time,
                event_time_precision,
                source_client,
            ),
        )
        # 写入即索引（v8 §3.1）：ngram 派生与事实写入同一事务，不允许「写进去了搜不到」。
        # 仍保留 fts 派生水位（重复执行幂等），派生层可整体清空重跑。
        rowid = conn.execute(
            "SELECT gseq FROM memories WHERE id=?", (memory_id,)
        ).fetchone()[0]
        ngrams = textnorm.ngram_text(content)
        conn.execute(
            "INSERT OR REPLACE INTO memories_ngrams(memory_id, ngram_text) "
            "VALUES (?, ?)",
            (memory_id, ngrams),
        )
        conn.execute("DELETE FROM memories_fts_bigram WHERE rowid=?", (rowid,))
        conn.execute(
            "INSERT INTO memories_fts_bigram(rowid, ngram_text) VALUES (?, ?)",
            (rowid, ngrams),
        )
        conn.execute("DELETE FROM memories_fts_trigram WHERE rowid=?", (rowid,))
        conn.execute(
            "INSERT INTO memories_fts_trigram(rowid, content) VALUES (?, ?)",
            (rowid, textnorm.normalize(content)),
        )

    return {
        "id": memory_id,
        "seq": seq,
        "recorded_at": recorded_at,
        "client_write_id": client_write_id,
    }


def record_supersede(
    db: Database,
    *,
    from_id: str,
    to_id: str,
    kind: str,
    source_client: str,
    reason: str | None = None,
) -> dict:
    """追加一条作废边。from_id = 裁决记录 id，to_id = 被作用记录 id。

    v8 §4.4：新增 **supersede** 边若在「最新边为 supersede 的图」上成环（含自环），
    同一写事务内拒绝并返回 409；revoke 边不参与检测。
    """
    if kind not in ("supersede", "revoke"):
        raise HTTPException(status_code=422, detail=f"kind 非法: {kind!r}")
    with db.write_txn() as conn:
        for label, mid in (("from_id", from_id), ("to_id", to_id)):
            exists = conn.execute(
                "SELECT 1 FROM memories WHERE id=?", (mid,)
            ).fetchone()
            if exists is None:
                raise HTTPException(
                    status_code=422, detail=f"{label}={mid!r} 在 memories 中不存在"
                )
        if kind == "supersede" and voided_mod.would_create_cycle(conn, from_id, to_id):
            logger.warning(
                "拒绝成环的 supersede 边：原始错误 cycle detected from=%s to=%s",
                from_id,
                to_id,
            )
            raise HTTPException(
                status_code=409,
                detail="该裁决会形成循环，已拒绝",
            )
        recorded_at = now_ms()
        try:
            conn.execute(
                "INSERT INTO supersedes "
                "(from_id, to_id, kind, reason, recorded_at, source_client) "
                "VALUES (?,?,?,?,?,?)",
                (from_id, to_id, kind, reason, recorded_at, source_client),
            )
        except sqlite3.IntegrityError:
            # 原始 SQLite 错误只进服务端日志；HTTP 响应不泄露数据库细节。
            logger.exception("supersede insert integrity error")
            raise HTTPException(
                status_code=409,
                detail="该作废边与现有记录冲突，请重试（服务端已记录原始错误）",
            ) from None
    return {
        "from_id": from_id,
        "to_id": to_id,
        "kind": kind,
        "recorded_at": recorded_at,
    }
