"""作废过滤（per-pair 最新边语义）、唯一 hidden 集合、纠正者链的共享 SQL helper。

语义（docs/01-事实层设计.md + docs/02 检索层设计 v8 §4）：

- 对每个 ``(from_id, to_id)`` 取**最新边**：**只按自增 ``id DESC``**，不依赖
  ``recorded_at``（时钟回拨会选错边）。最新边 ``kind='supersede'`` → 该 ``to_id``
  进入 ``voided_initial``；``kind='revoke'`` → 恢复。
- **hidden（全系统唯一口径）**：
  ``hidden = { x ∈ voided_initial : 沿纠正者边 ≤20 跳内可达某个 ∉ voided_initial 的节点 }``
  - 可达的存活节点叫 x 的 **live heads**（可能多个，并列继承名次）。
  - 到不了存活节点（成环 / 20 跳触顶）的 voided 节点**不进 hidden**，进入 pardoned，
    按「循环裁决 / 链过长」展示。
- 反例：A↔B 成环、外部 C 纠正 A 时，A 与 B 的真实链头都是 C（B→A→C），都应替换为 C。
- 写入拒环只看「最新边为 supersede 的图」，revoke 边不参与；仅在新增 supersede 边时检测。

**禁止** ``NOT IN (SELECT to_id FROM supersedes WHERE kind='supersede')``：它忽略 revoke、
也忽略同 pair 后来的 revoke。所有调用方必须复用本模块。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from .db import Database

CHAIN_MAX_HOPS = 20

# 每个 (from_id,to_id) 的最新边 → 当前作废集合。窗口函数需要 SQLite >= 3.25。
# 最新边只按 id DESC（v8 §6：不依赖 recorded_at）。
VOIDED_CTE = """
WITH latest_edges AS (
  SELECT from_id, to_id, kind, recorded_at, id,
         ROW_NUMBER() OVER (
           PARTITION BY from_id, to_id
           ORDER BY id DESC
         ) AS rn
  FROM supersedes
),
voided AS (
  SELECT to_id FROM latest_edges WHERE rn = 1 AND kind = 'supersede'
)
"""

# 供各路 SQL 直接拼接（表别名固定为 m）。v8 起检索路**不得**在 SQL/mask 阶段排除
# hidden，这两个片段只用于读取判定与历史兼容，不参与各路 top-K。
NOT_VOIDED_SQL = "NOT EXISTS (SELECT 1 FROM voided v WHERE v.to_id = m.id)"
IS_VOIDED_SQL = "EXISTS (SELECT 1 FROM voided v WHERE v.to_id = m.id)"


@dataclass
class HiddenSnapshot:
    """同一只读快照内的作废/隐藏/纠正链结果。"""

    voided: set[str] = field(default_factory=set)
    hidden: set[str] = field(default_factory=set)
    heads: dict[str, list[str]] = field(default_factory=dict)
    cycle_members: set[str] = field(default_factory=set)
    overlong_chain: set[str] = field(default_factory=set)
    cycle_components: list[list[str]] = field(default_factory=list)

    @property
    def pardoned(self) -> set[str]:
        return self.cycle_members | self.overlong_chain


def _latest_active_edges(
    conn: sqlite3.Connection,
) -> tuple[dict[str, list[str]], set[str]]:
    """返回 (adjacency, voided)。

    ``adjacency[to_id]`` = 指向它的纠正者 from_id 列表（按边 id 新→旧）。
    只包含「该 pair 最新边为 supersede」的边。
    """
    rows = conn.execute(
        "SELECT from_id, to_id, kind, id FROM supersedes ORDER BY id"
    ).fetchall()
    latest: dict[tuple[str, str], tuple[str, str, str, int]] = {}
    for row in rows:  # 升序遍历，后写覆盖前写 = 该 pair 最新
        from_id, to_id, kind, edge_id = row[0], row[1], row[2], int(row[3])
        latest[(from_id, to_id)] = (from_id, to_id, kind, edge_id)
    adjacency: dict[str, list[tuple[int, str]]] = {}
    voided: set[str] = set()
    for from_id, to_id, kind, edge_id in latest.values():
        if kind != "supersede":
            continue
        voided.add(to_id)
        adjacency.setdefault(to_id, []).append((edge_id, from_id))
    ordered: dict[str, list[str]] = {
        to_id: [from_id for _eid, from_id in sorted(items, reverse=True)]
        for to_id, items in adjacency.items()
    }
    return ordered, voided


def voided_id_set(conn: sqlite3.Connection) -> set[str]:
    """当前全部作废 id（一次查询，供内存侧过滤/纠正链判定复用）。"""
    rows = conn.execute(VOIDED_CTE + " SELECT to_id FROM voided").fetchall()
    return {row[0] for row in rows}


def is_voided_id(conn: sqlite3.Connection, memory_id: str) -> bool:
    row = conn.execute(
        VOIDED_CTE + " SELECT 1 FROM voided WHERE to_id = ? LIMIT 1", (memory_id,)
    ).fetchone()
    return row is not None


def superseding_from_ids(conn: sqlite3.Connection, to_id: str) -> list[str]:
    """指向 ``to_id`` 且其最新边为 supersede 的裁决记录 id（纠正者来源）。

    同一 pair 有多条边时只认**最新一条（id DESC）**；返回值按边 id 新→旧。
    """
    rows = conn.execute(
        """
        WITH latest_edges AS (
          SELECT from_id, to_id, kind, id,
                 ROW_NUMBER() OVER (
                   PARTITION BY from_id, to_id
                   ORDER BY id DESC
                 ) AS rn
          FROM supersedes
          WHERE to_id = ?
        )
        SELECT from_id, id FROM latest_edges
        WHERE rn = 1 AND kind = 'supersede'
        ORDER BY id DESC
        """,
        (to_id,),
    ).fetchall()
    return [row[0] for row in rows]


def _walk_heads(
    origin_id: str,
    adjacency: dict[str, list[str]],
    voided: set[str],
    *,
    max_hops: int = CHAIN_MAX_HOPS,
) -> tuple[list[str], bool]:
    """从 ``origin_id`` 沿纠正者边向上走，返回 (存活链头列表, 是否 20 跳触顶)。

    - 存活链头 = 任意深度（≤max_hops）遇到的 ∉ voided 节点；多个并列保留。
    - 不丢弃：触顶时已到达的节点照常返回，``exhausted=True`` 表示还有更上层未走到。
    - 环安全：``seen`` 去重，A↔B 不会死循环。
    """
    seen: set[str] = {origin_id}
    frontier: list[str] = [origin_id]
    heads: list[str] = []
    for _depth in range(max_hops):
        next_frontier: list[str] = []
        for node in frontier:
            for corrector in adjacency.get(node, ()):
                if corrector in seen:
                    continue
                seen.add(corrector)
                if corrector not in voided:
                    heads.append(corrector)
                else:
                    next_frontier.append(corrector)
        if not next_frontier:
            return heads, False
        frontier = next_frontier
    return heads, True


def live_heads(
    conn: sqlite3.Connection,
    origin_id: str,
    *,
    max_hops: int = CHAIN_MAX_HOPS,
    snapshot: HiddenSnapshot | None = None,
) -> list[str]:
    """某节点（应为 voided 成员）的存活纠正者链头；多个并列保留、去重保序。

    不可达存活节点时返回空列表（该节点不进 hidden，按循环/链过长展示）。
    """
    if snapshot is not None:
        return list(snapshot.heads.get(origin_id, []))
    adjacency, voided = _latest_active_edges(conn)
    heads, _exhausted = _walk_heads(origin_id, adjacency, voided, max_hops=max_hops)
    return _dedupe(heads)


def correction_chain(
    conn: sqlite3.Connection,
    origin_id: str,
    *,
    max_hops: int = CHAIN_MAX_HOPS,
    voided: set[str] | None = None,
) -> tuple[list[str], int]:
    """兼容旧名：返回 (存活链头列表, 0)。v8 起 20 跳且**不丢弃**。

    第二个返回值保留为 0（旧接口的「超限丢弃数」在 v8 已取消；链过长由
    :func:`hidden_snapshot` 单独标记，不静默丢）。
    """
    if voided is None:
        return live_heads(conn, origin_id, max_hops=max_hops), 0
    adjacency, _voided = _latest_active_edges(conn)
    heads, _exhausted = _walk_heads(
        origin_id, adjacency, set(voided), max_hops=max_hops
    )
    return _dedupe(heads), 0


def hidden_snapshot(conn: sqlite3.Connection) -> HiddenSnapshot:
    """在**一个只读查询**内算出 voided / hidden / 链头 / pardoned。

    调用方应把它当作一次快照使用（检索全程复用同一份，避免边在查询中途变化）。
    """
    adjacency, voided = _latest_active_edges(conn)
    snapshot = HiddenSnapshot(voided=set(voided))
    for origin in voided:
        heads, exhausted = _walk_heads(origin, adjacency, voided)
        if heads:
            snapshot.hidden.add(origin)
            snapshot.heads[origin] = _dedupe(heads)
        elif exhausted:
            snapshot.overlong_chain.add(origin)
        else:
            snapshot.cycle_members.add(origin)
    snapshot.cycle_components = _connected_components(adjacency, snapshot.cycle_members)
    return snapshot


def _connected_components(
    adjacency: dict[str, list[str]], members: set[str]
) -> list[list[str]]:
    """把环成员按（无向）纠正者边分组成连通分量。"""
    if not members:
        return []
    neighbors: dict[str, set[str]] = {member: set() for member in members}
    for to_node, from_nodes in adjacency.items():
        if to_node not in members:
            continue
        for from_node in from_nodes:
            if from_node in members:
                neighbors[to_node].add(from_node)
                neighbors[from_node].add(to_node)
    seen: set[str] = set()
    components: list[list[str]] = []
    for member in sorted(members):
        if member in seen:
            continue
        stack = [member]
        seen.add(member)
        group: list[str] = []
        while stack:
            node = stack.pop()
            group.append(node)
            for nxt in neighbors.get(node, ()):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        components.append(sorted(group))
    return components


def hidden_id_set(conn: sqlite3.Connection) -> set[str]:
    return hidden_snapshot(conn).hidden


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def would_create_cycle(
    conn: sqlite3.Connection, from_id: str, to_id: str
) -> bool:
    """新增 supersede 边 ``from_id → to_id`` 是否会在最新 supersede 图上成环。

    - 自环（from == to）直接成环。
    - 只考虑「最新边为 supersede」的图；revoke 边不参与。
    - 若图中已存在同一 (from,to) 的旧 supersede 边，先用新边替换再判定。
    """
    if from_id == to_id:
        return True
    adjacency, _voided = _latest_active_edges(conn)
    # 去掉同一 pair 的旧边（新边会替换它），再加新边
    graph: dict[str, set[str]] = {}
    for to_node, from_nodes in adjacency.items():
        for from_node in from_nodes:
            if from_node == from_id and to_node == to_id:
                continue
            graph.setdefault(from_node, set()).add(to_node)
    graph.setdefault(from_id, set()).add(to_id)

    # 成环 ⇔ 从 to_id 出发能回到 from_id
    stack = [to_id]
    seen = {to_id}
    while stack:
        node = stack.pop()
        for nxt in graph.get(node, ()):
            if nxt == from_id:
                return True
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return False
