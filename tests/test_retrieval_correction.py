"""hidden / 路内替换（v8 §4）：纠正者继承名次、20 跳、不丢弃、环裁决、多纠正者。"""

from app import voided as voided_mod

from tests.retrieval_helpers import (
    add_edge,
    add_memory,
    build_derived,
    make_service,
)


def _chain(db, length: int):
    """target 被 b1 纠正，b1 被 b2 纠正……返回 (target, [b1..blength])。"""
    target = add_memory(db, "我的车是蓝色", session_id="car")
    nodes = [target]
    for i in range(length):
        nodes.append(add_memory(db, f"第 {i + 1} 次改成别的颜色", session_id=f"c{i}"))
    for i in range(length):
        add_edge(db, nodes[i + 1], nodes[i], kind="supersede", recorded_at=100 + i)
    return target, nodes[1:]


def test_corrector_inherits_rank_and_is_tagged(db):
    target = add_memory(db, "我的车是蓝色", session_id="car")
    corrector = add_memory(db, "其实应该是白色", session_id="correction")
    add_edge(db, corrector, target, kind="supersede", recorded_at=100)
    build_derived(db)

    service = make_service(db, routes=("bigram",))
    result = service.search("车是什么颜色")
    ids = [item["id"] for item in result["items"]]
    assert target not in ids  # hidden 已替换，不进输出
    assert corrector in ids  # 纠正者占 A 的原始名次
    item = next(i for i in result["items"] if i["id"] == corrector)
    assert item["routes"] == ["bigram"]
    assert f"superseded-by:{target}" in item["tags"]
    assert item["score"] == round(1 / 61, 12)  # 名次 1 → RRF 分
    assert result["hidden"]["matched"] == 1


def test_live_chain_reaches_20_hops(db):
    target, nodes = _chain(db, 20)
    build_derived(db)
    voided = voided_mod.voided_id_set(db.read_conn)
    heads = voided_mod.live_heads(db.read_conn, target, max_hops=20)
    assert heads == [nodes[-1]]
    assert target in voided_mod.hidden_id_set(db.read_conn)

    service = make_service(db, routes=("bigram",))
    result = service.search("车是什么颜色")
    ids = [item["id"] for item in result["items"]]
    assert nodes[-1] in ids
    assert target not in ids and all(node not in ids for node in nodes[:-1])


def test_chain_over_20_hops_is_not_hidden_and_not_discarded(db):
    target, nodes = _chain(db, 21)
    build_derived(db)
    snapshot = voided_mod.hidden_snapshot(db.read_conn)
    # 20 跳内到不了存活节点 → 不进 hidden，按「链过长」展示
    assert target not in snapshot.hidden
    assert target in snapshot.overlong_chain
    # 20 跳终点的 b20 仍可达 b21（1 跳），它是 hidden
    assert nodes[19] in snapshot.hidden
    assert snapshot.heads[nodes[19]] == [nodes[20]]

    service = make_service(db, routes=("bigram",))
    result = service.search("车是什么颜色")
    ids = [item["id"] for item in result["items"]]
    # target 不是 hidden（不替换、不丢弃），但标记链过长
    assert target in ids
    target_item = next(i for i in result["items"] if i["id"] == target)
    assert "chain-too-long" in target_item["tags"]


def test_cycle_members_are_not_hidden(db):
    a = add_memory(db, "A 记录", session_id="s1")
    b = add_memory(db, "B 记录", session_id="s2")
    add_edge(db, a, b, kind="supersede", recorded_at=100)
    add_edge(db, b, a, kind="supersede", recorded_at=110)
    snapshot = voided_mod.hidden_snapshot(db.read_conn)
    assert snapshot.voided == {a, b}
    assert snapshot.hidden == set()
    assert snapshot.cycle_members == {a, b}


def test_cycle_with_external_corrector_maps_both_to_c(db):
    """反例（v8 §4.1）：A↔B 成环、C 纠正 A 时，A 与 B 的真实链头都是 C。"""
    a = add_memory(db, "A 记录", session_id="s1")
    b = add_memory(db, "B 记录", session_id="s2")
    c = add_memory(db, "C 是最终版本", session_id="s3")
    add_edge(db, a, b, kind="supersede", recorded_at=100)
    add_edge(db, b, a, kind="supersede", recorded_at=110)
    add_edge(db, c, a, kind="supersede", recorded_at=120)
    snapshot = voided_mod.hidden_snapshot(db.read_conn)
    assert snapshot.hidden == {a, b}
    assert snapshot.heads[a] == [c]
    assert snapshot.heads[b] == [c]

    build_derived(db)
    result = make_service(db, routes=("bigram",)).search("A 记录")
    ids = [item["id"] for item in result["items"]]
    assert c in ids
    assert a not in ids and b not in ids
    item = next(i for i in result["items"] if i["id"] == c)
    assert f"superseded-by:{a}" in item["tags"]


def test_corrector_receives_votes_from_every_route_that_hit_target(db):
    """A 被两路命中 → 纠正者在两路各得一票（跨路各一票是正当证据）。"""
    target = add_memory(db, "北京项目计划书", session_id="s1")
    corrector = add_memory(db, "计划书改期了", session_id="s9")
    add_edge(db, corrector, target, kind="supersede", recorded_at=100)
    build_derived(db)
    result = make_service(db, routes=("bigram", "trigram")).search("北京项目计划书")
    item = next(i for i in result["items"] if i["id"] == corrector)
    assert set(item["routes"]) == {"bigram", "trigram"}
    assert item["score"] == round(1 / 61 + 1 / 61, 12)


def test_multiple_correctors_all_keep_and_inherit_same_rank(db):
    target = add_memory(db, "我的车是蓝色", session_id="car")
    b1 = add_memory(db, "其实应该是白色", session_id="c1")
    b2 = add_memory(db, "也有人说黑色", session_id="c2")
    add_edge(db, b1, target, kind="supersede", recorded_at=100)
    add_edge(db, b2, target, kind="supersede", recorded_at=110)
    build_derived(db)

    result = make_service(db, routes=("bigram",)).search("车是什么颜色")
    ids = [item["id"] for item in result["items"]]
    assert b1 in ids and b2 in ids
    # 并列继承 A 的名次 → 同分；输出顺序由 gseq DESC 决定（新者 b2 在前）
    scores = {item["id"]: item["score"] for item in result["items"]}
    assert scores[b1] == scores[b2] == round(1 / 61, 12)
    assert ids.index(b2) < ids.index(b1)


def test_revoked_correction_edge_is_not_a_corrector(db):
    target = add_memory(db, "我的车是蓝色", session_id="car")
    b = add_memory(db, "其实应该是白色", session_id="c1")
    add_edge(db, b, target, kind="supersede", recorded_at=100)
    add_edge(db, b, target, kind="revoke", recorded_at=200)
    build_derived(db)
    snapshot = voided_mod.hidden_snapshot(db.read_conn)
    assert target not in snapshot.hidden
    result = make_service(db, routes=("bigram",)).search("车是什么颜色")
    assert target in [item["id"] for item in result["items"]]


def test_chain_unit_helpers_20_hops_no_discard(db):
    a = add_memory(db, "A", session_id="s1")
    b = add_memory(db, "B", session_id="s2")
    c = add_memory(db, "C", session_id="s3")
    d = add_memory(db, "D", session_id="s4")
    add_edge(db, b, a, kind="supersede", recorded_at=100)
    add_edge(db, c, b, kind="supersede", recorded_at=110)
    add_edge(db, d, c, kind="supersede", recorded_at=120)
    voided = voided_mod.voided_id_set(db.read_conn)
    kept, dropped = voided_mod.correction_chain(db.read_conn, a, max_hops=20, voided=voided)
    assert kept == [d]
    assert dropped == 0  # v8 不再丢弃
    kept, dropped = voided_mod.correction_chain(db.read_conn, a, max_hops=2, voided=voided)
    assert kept == []  # 2 跳内仍是 voided；但它们不是丢弃
    assert dropped == 0
    assert voided_mod.superseding_from_ids(db.read_conn, a) == [b]


# ---- 历史环（v8 §4.4 / §9.6）：只算一票、最多展示 3 个、其余列 id -------------


def test_historical_cycle_counts_one_vote_and_tags_all_members(db):
    a = add_memory(db, "A 记录", session_id="s1")
    b = add_memory(db, "B 记录", session_id="s2")
    add_edge(db, a, b, kind="supersede", recorded_at=100)
    add_edge(db, b, a, kind="supersede", recorded_at=110)
    build_derived(db)
    result = make_service(db, routes=("bigram",)).search("记录")
    ids = [item["id"] for item in result["items"]]
    assert b in ids and a in ids  # 最新成员在前
    assert all("cycle" in item["tags"] for item in result["items"])
    # 环整体只算一票：成员共享同一 RRF 分，不因多条成员重复计分
    scores = {item["score"] for item in result["items"]}
    assert scores == {round(1 / 61, 12)}


def test_historical_cycle_displays_at_most_three_members_and_lists_rest(db):
    nodes = [add_memory(db, f"{chr(65 + i)} 记录", session_id=f"s{i}") for i in range(4)]
    # A→B→C→D→A 成环（历史数据）
    for i in range(4):
        add_edge(db, nodes[i], nodes[(i + 1) % 4], kind="supersede", recorded_at=100 + i)
    build_derived(db)
    result = make_service(db, routes=("bigram",)).search("记录")
    assert len(result["items"]) == 3  # FINAL_K=4 下最多展示 3 个
    assert all("cycle" in item["tags"] for item in result["items"])
    omitted = [mid for mid in nodes if mid not in {i["id"] for i in result["items"]}]
    assert len(omitted) == 1
    assert omitted[0] in result["items"][-1]["content"]  # 其余列 id 清单
    assert "循环裁决" in result["items"][-1]["content"]
