"""derive 水位循环：推进、失败计数、全量重建（单写事务）。"""

from app.derive import DerivePipeline
from tests.conftest import prime_health, write_one


def _seed(client, n=3):
    ids = []
    for i in range(n):
        resp = write_one(client, cwid=f"cw-derive-{i}", content=f"d{i}", session_id="sd")
        assert resp.status_code == 200
        ids.append(resp.json()["id"])
    return ids


def test_pending_counts_then_tick_completes(client, db):
    _seed(client)
    pipe = DerivePipeline(db, stub_success=True)
    for stage in ("fts", "embedding", "entity", "graph"):
        assert pipe.pending_count(stage) == 3
    processed = pipe.tick()
    assert processed == {"fts": 3, "embedding": 3, "entity": 3, "graph": 3}
    for stage in ("fts", "embedding", "entity", "graph"):
        assert pipe.pending_count(stage) == 0
        assert pipe.failures[stage] == 0
    done = db.read_conn.execute("SELECT COUNT(*) FROM derive_state").fetchone()[0]
    assert done == 12  # 3 条 × 4 stage


def test_failed_processor_counts_and_stays_pending(db):
    from app.write_path import create_memory

    for i in range(2):
        create_memory(
            db,
            client_write_id=f"cw-fail-{i}",
            content=f"f{i}",
            session_id="sf",
            source_client="t",
        )
    now = [0.0]
    pipe = DerivePipeline(db, stub_success=True, clock=lambda: now[0])
    pipe.register("embedding", lambda m: (_ for _ in ()).throw(RuntimeError("boom")))
    pipe.tick()
    # 失败后指数退避（1s）：本轮立即停手，不把剩下的 pending 全打一遍 API。
    assert pipe.failures["embedding"] == 1
    assert pipe.pending_count("embedding") == 2  # 失败不写 done，停留未完成
    assert pipe.pending_count("entity") == 0
    assert pipe.pending_count("graph") == 0
    now[0] = 2.0  # 越过 1s 退避
    pipe.tick()
    assert pipe.failures["embedding"] == 2
    assert pipe.pending_count("embedding") == 2
    pipe.register("embedding", lambda m: None)
    now[0] = 10.0  # 越过第二次失败后的 2s 退避
    pipe.tick()
    assert pipe.pending_count("embedding") == 0


def test_stub_fail_via_config(db):
    from app.write_path import create_memory

    create_memory(
        db, client_write_id="cw-stubfail", content="x",
        session_id="ss", source_client="t",
    )
    pipe = DerivePipeline(db, stub_success=False)
    pipe.tick()
    assert pipe.failures["embedding"] == 1


def test_rebuild_all_is_single_transaction_and_refills(db, client):
    ids = _seed(client)
    pipe = DerivePipeline(db, stub_success=True)
    pipe.tick()
    assert pipe.pending_count("fts") == 0
    counts = pipe.rebuild_all()
    assert counts == {"fts": 3, "embedding": 3, "entity": 3, "graph": 3}
    # 单事务重建完成后没有 pending，派生数据已全部就位
    for stage in ("fts", "embedding", "entity", "graph"):
        assert pipe.pending_count(stage) == 0
    assert db.read_conn.execute("SELECT COUNT(*) FROM derive_state").fetchone()[0] == 12
    assert db.read_conn.execute("SELECT COUNT(*) FROM memories_ngrams").fetchone()[0] == 3
    assert (
        db.read_conn.execute("SELECT COUNT(*) FROM memories_fts_bigram").fetchone()[0]
        == 3
    )
    assert (
        db.read_conn.execute("SELECT COUNT(*) FROM memories_fts_trigram").fetchone()[0]
        == 3
    )
    # 再次 rebuild 仍清空并重建
    with db.write_txn() as conn:
        conn.execute("INSERT INTO embeddings VALUES (?,?,?,?)", (ids[0], b"x", "", 0))
        conn.execute("INSERT INTO entities VALUES (?,?)", (ids[0], "e1"))
    pipe.rebuild_all()
    assert db.read_conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == 0
    assert db.read_conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 0
    assert db.read_conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 3


def test_rebuild_failure_keeps_old_derived_index(db, client):
    """重建先算 embedding、后开写事务：provider 失败时旧索引原样保留。"""
    _seed(client)
    pipe = DerivePipeline(db, stub_success=True)
    pipe.tick()
    before = {
        "ngrams": db.read_conn.execute("SELECT COUNT(*) FROM memories_ngrams").fetchone()[0],
        "fts": db.read_conn.execute("SELECT COUNT(*) FROM memories_fts_bigram").fetchone()[0],
        "derive": db.read_conn.execute("SELECT COUNT(*) FROM derive_state").fetchone()[0],
    }
    assert before == {"ngrams": 3, "fts": 3, "derive": 12}

    class Boom:
        model_id = "boom"

        def embed(self, texts):
            raise RuntimeError("embedding down")

    bad = DerivePipeline(db, stub_success=True, embedding_provider=Boom())
    import pytest

    with pytest.raises(RuntimeError):
        bad.rebuild_all()
    after = {
        "ngrams": db.read_conn.execute("SELECT COUNT(*) FROM memories_ngrams").fetchone()[0],
        "fts": db.read_conn.execute("SELECT COUNT(*) FROM memories_fts_bigram").fetchone()[0],
        "derive": db.read_conn.execute("SELECT COUNT(*) FROM derive_state").fetchone()[0],
    }
    assert after == before  # 没有部分写入


def test_derive_tick_endpoint_and_health_integration(client):
    _seed(client, n=2)
    before = prime_health(client)
    assert before["pending"]["embedding"] == 2
    assert before["pending"]["fts"] == 2
    processed = client.post("/derive/tick").json()
    assert processed["embedding"] == 2
    after = prime_health(client)
    assert after["pending"] == {"fts": 0, "embedding": 0, "entity": 0, "graph": 0}


def test_embedding_failure_counts_alerts_and_redacts(client):
    """processor 抛错：failures+1、/health.alerts 可见、pending 不减、错误脱敏。"""
    write_one(client, cwid="cw-embfail", content="失败记录", session_id="se")
    pipe = client.app.state.pipeline

    def boom(memory):
        raise RuntimeError("embedding down (api_key=sk-secret123456789)")

    pipe.register("embedding", boom)
    pipe.tick()
    assert pipe.failures["embedding"] == 1
    assert pipe.pending_count("embedding") == 1

    health = prime_health(client)
    assert health["failures"]["embedding"] == 1
    assert health["pending"]["embedding"] == 1
    assert "embedding_failures>0" in health["alerts"]
    last_error = health["embedding"]["last_error"] or ""
    assert "sk-secret123456789" not in last_error
    assert "<redacted>" in last_error
    assert health["embedding"]["circuit"]["circuit_open"] is False


def test_embedding_circuit_opens_after_three_failures_then_recovers(db):
    """连续 3 次失败 → 熔断 5 分钟；期间跳过并计数；恢复后继续补派生。"""
    from app.health import health_report
    from app.write_path import create_memory

    class FlakyProvider:
        model_id = "flaky-test"

        def __init__(self):
            self.fail = True
            self.calls = 0

        def embed(self, texts):
            self.calls += 1
            if self.fail:
                raise RuntimeError("provider down")
            return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    provider = FlakyProvider()
    create_memory(
        db,
        client_write_id="cw-circuit",
        content="熔断测试",
        session_id="sc",
        source_client="t",
    )
    now = [0.0]
    pipe = DerivePipeline(db, embedding_provider=provider, clock=lambda: now[0])

    pipe.tick()  # 失败 1，退避 1s
    assert pipe.failures["embedding"] == 1
    now[0] = 1.5
    pipe.tick()  # 失败 2，退避 2s
    assert pipe.failures["embedding"] == 2
    now[0] = 4.0
    pipe.tick()  # 失败 3 → 熔断
    assert pipe.failures["embedding"] == 3
    assert pipe.embedding_state()["circuit"]["circuit_open"] is True

    calls_before_skip = provider.calls
    now[0] = 5.0
    pipe.tick()
    assert provider.calls == calls_before_skip  # 熔断期间不再调 API
    assert pipe.embedding_circuit_skips == 1
    assert pipe.pending_count("embedding") == 1

    health = health_report(db, pipe)
    assert "embedding_failures>0" in health["alerts"]
    assert "embedding_circuit_open" in health["alerts"]
    assert health["embedding"]["circuit"]["circuit_open"] is True
    assert health["embedding"]["circuit_skips"] == 1

    # 熔断窗口过去 + provider 恢复：同一 pending 条目继续派生成功。
    provider.fail = False
    now[0] = 310.0
    pipe.tick()
    assert pipe.pending_count("embedding") == 0
    assert pipe.embedding_circuit.state()["circuit_open"] is False
    assert pipe.embedding_circuit.state()["consecutive_failures"] == 0
    assert pipe.embedding_state()["failures"] == 3  # 累计失败保留，便于审计
