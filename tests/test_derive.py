"""derive 水位循环：推进、失败计数、全量重建（单写事务）。"""

from app.derive import DerivePipeline
from tests.conftest import write_one


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
    pipe = DerivePipeline(db, stub_success=True)
    pipe.register("embedding", lambda m: (_ for _ in ()).throw(RuntimeError("boom")))
    pipe.tick()
    assert pipe.failures["embedding"] == 2
    assert pipe.pending_count("embedding") == 2  # 失败不写 done，停留未完成
    assert pipe.pending_count("entity") == 0
    assert pipe.pending_count("graph") == 0
    pipe.register("embedding", lambda m: None)
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
    before = client.get("/health").json()
    assert before["pending"]["embedding"] == 2
    assert before["pending"]["fts"] == 2
    processed = client.post("/derive/tick").json()
    assert processed["embedding"] == 2
    after = client.get("/health").json()
    assert after["pending"] == {"fts": 0, "embedding": 0, "entity": 0, "graph": 0}
