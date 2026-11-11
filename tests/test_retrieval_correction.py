"""R5 纠正后补：候选被剔除 → 纠正者继承名次；追链 3 跳；多纠正者并存；超限计数。"""

from app import voided as voided_mod

from tests.retrieval_helpers import (
    add_edge,
    add_memory,
    build_derived,
    make_service,
)


def _filler(db, count: int = 8) -> None:
    """更晚写入的无关记录：让 R6 兜底挑不到早先的纠正者，隔离 R5 行为。"""
    for i in range(count):
        add_memory(db, f"无关填充记录 {i}", session_id=f"filler-{i}")


def test_corrector_supplements_dropped_candidate(db):
    target = add_memory(db, "我的车是蓝色", session_id="car")
    corrector = add_memory(db, "其实应该是白色", session_id="correction")
    add_edge(db, corrector, target, kind="supersede", recorded_at=100)
    _filler(db)
    build_derived(db)

    service = make_service(db, routes=("bigram",))
    result = service.search("车是什么颜色")
    ids = [item["id"] for item in result["items"]]
    assert target not in ids  # 被作废，不在任何一路
    assert corrector in ids  # 纠正者补位
    item = next(i for i in result["items"] if i["id"] == corrector)
    assert item["routes"] == ["correction"]
    assert item["inherited_rank"] == 1
    assert item["inherited_from"] == target
    assert item["score"] is None  # 不参与 RRF 打分
    assert result["correction"]["candidates"] == 1
    assert result["correction"]["supplemented"] == 1


def test_correction_chain_three_hops(db):
    target = add_memory(db, "我的车是蓝色", session_id="car")
    b = add_memory(db, "改成白色", session_id="c1")
    c = add_memory(db, "又改成黑色", session_id="c2")
    d = add_memory(db, "最终是银色", session_id="c3")
    add_edge(db, b, target, kind="supersede", recorded_at=100)
    add_edge(db, c, b, kind="supersede", recorded_at=110)
    add_edge(db, d, c, kind="supersede", recorded_at=120)
    _filler(db)
    build_derived(db)

    service = make_service(db, routes=("bigram",))
    result = service.search("车是什么颜色")
    ids = [item["id"] for item in result["items"]]
    assert d in ids  # 第 3 跳的纠正者
    assert b not in ids and c not in ids  # 中间被再纠正的纠正者被过滤
    assert result["correction"]["supplemented"] == 1


def test_correction_chain_over_limit_dropped_and_counted(db):
    target = add_memory(db, "我的车是蓝色", session_id="car")
    b = add_memory(db, "改成白色", session_id="c1")
    c = add_memory(db, "又改成黑色", session_id="c2")
    d = add_memory(db, "再改成灰色", session_id="c3")
    e = add_memory(db, "最终是银色", session_id="c4")
    add_edge(db, b, target, kind="supersede", recorded_at=100)
    add_edge(db, c, b, kind="supersede", recorded_at=110)
    add_edge(db, d, c, kind="supersede", recorded_at=120)
    add_edge(db, e, d, kind="supersede", recorded_at=130)  # 需要第 4 跳 → 丢弃
    _filler(db)
    build_derived(db)

    service = make_service(db, routes=("bigram",))
    result = service.search("车是什么颜色")
    ids = [item["id"] for item in result["items"]]
    assert e not in ids
    assert result["correction"]["chain_dropped"] == 1
    assert result["correction"]["supplemented"] == 0


def test_multiple_correctors_all_kept(db):
    target = add_memory(db, "我的车是蓝色", session_id="car")
    b1 = add_memory(db, "其实应该是白色", session_id="c1")
    b2 = add_memory(db, "也有人说黑色", session_id="c2")
    add_edge(db, b1, target, kind="supersede", recorded_at=100)
    add_edge(db, b2, target, kind="supersede", recorded_at=110)
    _filler(db)
    build_derived(db)

    service = make_service(db, routes=("bigram",))
    result = service.search("车是什么颜色")
    ids = [item["id"] for item in result["items"]]
    assert b1 in ids and b2 in ids  # 不判断哪条作数：并存全部保留
    assert result["correction"]["supplemented"] == 2


def test_revoked_correction_edge_is_not_a_corrector(db):
    target = add_memory(db, "我的车是蓝色", session_id="car")
    b = add_memory(db, "其实应该是白色", session_id="c1")
    add_edge(db, b, target, kind="supersede", recorded_at=100)
    add_edge(db, b, target, kind="revoke", recorded_at=200)  # 最新边 revoke → 不再作废
    _filler(db)
    build_derived(db)

    service = make_service(db, routes=("bigram",))
    result = service.search("车是什么颜色")
    ids = [item["id"] for item in result["items"]]
    assert target in ids  # A 恢复可检索
    assert result["correction"]["supplemented"] == 0


def test_chain_unit_helpers(db):
    a = add_memory(db, "A", session_id="s1")
    b = add_memory(db, "B", session_id="s2")
    c = add_memory(db, "C", session_id="s3")
    d = add_memory(db, "D", session_id="s4")
    add_edge(db, b, a, kind="supersede", recorded_at=100)
    add_edge(db, c, b, kind="supersede", recorded_at=110)
    add_edge(db, d, c, kind="supersede", recorded_at=120)
    voided = voided_mod.voided_id_set(db.read_conn)
    kept, dropped = voided_mod.correction_chain(db.read_conn, a, max_hops=3, voided=voided)
    assert kept == [d]  # 只有末端的 D 未被作废
    assert dropped == 0

    kept, dropped = voided_mod.correction_chain(db.read_conn, a, max_hops=2, voided=voided)
    assert kept == []
    assert dropped == 1  # C 在第 2 跳仍被作废 → 需要第 3 跳 → 超限

    assert voided_mod.superseding_from_ids(db.read_conn, a) == [b]
