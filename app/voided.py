"""作废过滤（per-pair 最新边语义）与 R5 纠正链的共享 SQL helper。

语义（docs/01-事实层设计.md + docs/02 §4 硬性规则 2）：

- 对每个 ``(from_id, to_id)`` 取 ``ORDER BY recorded_at DESC, id DESC`` 的**最新边**；
- 最新边 ``kind='supersede'`` → 该 ``to_id`` 作废；``kind='revoke'`` → 恢复；
- 逐 pair 独立：B 对 A 的 revoke 不能撤销 C 对 A 的 supersede。

**禁止** ``NOT IN (SELECT to_id FROM supersedes WHERE kind='supersede')``：它忽略 revoke、
也忽略同 pair 后来的 revoke。所有调用方必须复用下面的 CTE。
"""

from __future__ import annotations

import sqlite3

from .db import Database

# 每个 (from_id,to_id) 的最新边 → 当前作废集合。窗口函数需要 SQLite >= 3.25。
VOIDED_CTE = """
WITH latest_edges AS (
  SELECT from_id, to_id, kind, recorded_at, id,
         ROW_NUMBER() OVER (
           PARTITION BY from_id, to_id
           ORDER BY recorded_at DESC, id DESC
         ) AS rn
  FROM supersedes
),
voided AS (
  SELECT to_id FROM latest_edges WHERE rn = 1 AND kind = 'supersede'
)
"""

# 供各路 SQL 直接拼接（表别名固定为 m）
NOT_VOIDED_SQL = "NOT EXISTS (SELECT 1 FROM voided v WHERE v.to_id = m.id)"
IS_VOIDED_SQL = "EXISTS (SELECT 1 FROM voided v WHERE v.to_id = m.id)"


def voided_id_set(conn: sqlite3.Connection) -> set[str]:
    """当前全部作废 id（一次查询，供内存侧过滤/纠正链判定复用）。"""
    rows = conn.execute(VOIDED_CTE + " SELECT to_id FROM voided").fetchall()
    return {row[0] for row in rows}


def is_voided_id(conn: sqlite3.Connection, memory_id: str) -> bool:
    row = conn.execute(
        VOIDED_CTE + " SELECT 1 FROM voided WHERE to_id = ? LIMIT 1", (memory_id,)
    ).fetchone()
    return row is not None


def superseding_from_ids(
    conn: sqlite3.Connection, to_id: str
) -> list[str]:
    """指向 ``to_id`` 且其最新边为 supersede 的裁决记录 id（R5 纠正者来源）。

    同一 pair 有多条边时只认最新一条；同毫秒按自增 id 决胜。按时间新→旧、id 大→小。
    """
    rows = conn.execute(
        """
        WITH latest_edges AS (
          SELECT from_id, to_id, kind, recorded_at, id,
                 ROW_NUMBER() OVER (
                   PARTITION BY from_id, to_id
                   ORDER BY recorded_at DESC, id DESC
                 ) AS rn
          FROM supersedes
          WHERE to_id = ?
        )
        SELECT from_id FROM latest_edges
        WHERE rn = 1 AND kind = 'supersede'
        ORDER BY recorded_at DESC, id DESC
        """,
        (to_id,),
    ).fetchall()
    return [row[0] for row in rows]


def correction_chain(
    conn: sqlite3.Connection,
    origin_id: str,
    *,
    max_hops: int = 3,
    voided: set[str] | None = None,
) -> tuple[list[str], int]:
    """沿 supersedes 边从被作废的 ``origin_id`` 追纠正者，最多 ``max_hops`` 跳。

    - 纠正者自身过作废过滤：未作废 → 保留；已作废 → 继续沿它的纠正者追。
    - 多纠正者并存全部保留（同一层多个 from_id 都留）。
    - 第 ``max_hops`` 跳找到的纠正者若自身仍被作废，则它需要第 ``max_hops+1`` 跳 →
      **丢弃并计数**（返回值第二项）。
    - 返回 (保留的纠正者 id 列表, 因追链超限丢弃数)。顺序确定：按层、按边时间。
    """
    voided_set = voided if voided is not None else voided_id_set(conn)
    kept: list[str] = []
    dropped = 0
    seen: set[str] = {origin_id}
    frontier: list[str] = [origin_id]
    for depth in range(1, max_hops + 1):
        next_frontier: list[str] = []
        for node in frontier:
            for corrector in superseding_from_ids(conn, node):
                if corrector in seen:
                    continue
                seen.add(corrector)
                if corrector not in voided_set:
                    kept.append(corrector)
                elif depth == max_hops:
                    dropped += 1  # 需要第 max_hops+1 跳，超出上限
                else:
                    next_frontier.append(corrector)
        frontier = next_frontier
        if not frontier:
            break
    return kept, dropped
