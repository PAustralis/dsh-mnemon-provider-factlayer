"""v8 §7 / F：R1 默认关、按行 model_id+dim、无向量行排除、写后增量刷新、熔断。"""

from __future__ import annotations

import pytest

from app.config import load_config
from app.derive import DerivePipeline
from app.embeddings import EmbeddingModelMismatch, VectorIndex
from app.write_path import create_memory
from tests.fake_embedding import FailingEmbeddingProvider, HashingEmbeddingProvider
from tests.retrieval_helpers import add_memory, build_derived, make_service


def test_r1_is_off_by_default():
    cfg = load_config()
    assert "vector" not in cfg.route_names
    assert cfg.route_names == ("bigram",)
    assert cfg.embedding_max_spend_usd == 0.50


def test_enabling_vector_without_model_fails_at_startup(tmp_path, monkeypatch):
    from app.embeddings import EmbeddingConfigError
    from app.main import create_app

    monkeypatch.setenv("RETRIEVAL_ROUTES", "vector")
    monkeypatch.delenv("EMBEDDING_MODEL", raising=False)
    monkeypatch.delenv("DSH_EMBEDDING_MODEL", raising=False)
    monkeypatch.setenv("DSH_DERIVE_LOOP_ENABLED", "0")
    monkeypatch.setenv("DSH_BACKUP_INTERVAL_SECONDS", "0")
    with pytest.raises(EmbeddingConfigError):
        create_app(db_path=str(tmp_path / "vector-without-model.db"))


def test_embeddings_store_model_id_and_dim_per_row(db):
    provider = HashingEmbeddingProvider(dim=8)
    add_memory(db, "北京记录", session_id="s1")
    build_derived(db, embedding_provider=provider)
    rows = db.read_conn.execute(
        "SELECT model_id, dim FROM embeddings"
    ).fetchall()
    assert rows
    assert all(row["model_id"] == provider.model_id for row in rows)
    assert all(row["dim"] == 8 for row in rows)


def test_vector_route_returns_semantic_neighbour(db):
    provider = HashingEmbeddingProvider()
    semantic = add_memory(db, "向量语义近邻", session_id="s1")
    add_memory(db, "完全无关文本", session_id="s2")
    build_derived(db, embedding_provider=provider)
    service = make_service(db, routes=("vector",), provider=provider)
    result = service.search("向量语义近邻")
    # 哈希 provider 对同一文本给同一向量，命中自身
    assert semantic in [item["id"] for item in result["items"]]


def test_no_embedding_row_is_excluded_from_r1_candidates(db):
    provider = HashingEmbeddingProvider()
    embedded = add_memory(db, "有向量的记录", session_id="s1")
    build_derived(db, embedding_provider=provider)
    no_vector = add_memory(db, "没有向量的记录", session_id="s2")
    # 只补 fts，不跑 embedding
    pipeline = DerivePipeline(db, stub_success=True)
    pipeline._run_stage("fts")
    service = make_service(db, routes=("vector",), provider=provider)
    ids, matrix, gseqs = service.index.load(
        db, model_id=provider.model_id, dim=None
    )
    assert embedded in ids
    assert no_vector not in ids
    assert embedded in [item["id"] for item in service.search("有向量的记录")["items"]]


def test_write_commit_incrementally_refreshes_vector_matrix(db):
    provider = HashingEmbeddingProvider()
    first = add_memory(db, "第一条向量记录", session_id="s1")
    build_derived(db, embedding_provider=provider)
    service = make_service(db, routes=("vector",), provider=provider)
    # 先让 service 载入矩阵
    service.index.load(db, model_id=provider.model_id, dim=None)

    second = create_memory(
        db,
        client_write_id="cw-incremental",
        content="第二条向量记录",
        session_id="s2",
        source_client="t",
    )["id"]
    # pipeline 与 service 共享同一个 VectorIndex → 写事务后增量 upsert
    pipeline = DerivePipeline(
        db,
        stub_success=True,
        embedding_provider=provider,
        vector_index=service.index,
    )
    pipeline.tick()
    ids, _matrix, _gseqs = service.index.load(db, model_id=provider.model_id, dim=None)
    assert set(ids) == {first, second}
    result = service.search("第二条向量记录")
    assert second in [item["id"] for item in result["items"]]


def test_model_mismatch_raises_and_blocks_r1(db):
    provider = HashingEmbeddingProvider(model_id="model-a")
    add_memory(db, "北京记录", session_id="s1")
    build_derived(db, embedding_provider=provider)
    other = HashingEmbeddingProvider(model_id="model-b")
    service = make_service(db, routes=("vector", "bigram"), provider=other)
    with pytest.raises(EmbeddingModelMismatch):
        service.search("北京记录")


def test_vector_circuit_opens_after_three_failures(db):
    provider = HashingEmbeddingProvider()
    add_memory(db, "北京记录", session_id="s1")
    build_derived(db, embedding_provider=provider)

    class FailingSameModel(FailingEmbeddingProvider):
        model_id = provider.model_id

    failing = FailingSameModel()
    service = make_service(db, routes=("vector", "bigram"), provider=failing)
    for _ in range(3):
        result = service.search("北京记录")
        assert result["items"]  # bigram 照常
    assert service.vector_state()["circuit_open"] is True
    calls_after_open = failing.calls
    result = service.search("北京记录")
    assert result["routes"]["vector"]["skipped"] == "circuit_open"
    assert failing.calls == calls_after_open  # 熔断期间不再调 API
    assert service.vector_state()["circuit_open"] is True


def test_vector_index_gseq_alignment_and_tie_order(db):
    provider = HashingEmbeddingProvider()
    add_memory(db, "北京记录一", session_id="s1")
    add_memory(db, "北京记录二", session_id="s2")
    build_derived(db, embedding_provider=provider)
    index = VectorIndex()
    ids, matrix, gseqs = index.load(db, model_id=provider.model_id, dim=None)
    assert len(ids) == len(gseqs) == 2
    assert list(gseqs) == sorted(gseqs)  # gseq 升序对齐
    assert matrix.shape[0] == 2
