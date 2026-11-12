"""索引文本日期前缀（docs/02 v9）：纯函数、写入路径、版本化重建、幂等。

覆盖范围（对应实现要求 1/2/5）：

- 前缀生成：有 event_time（年/月/日/instant）、只有 recorded_at、空日期、非法日期；
- 索引文本：前缀 + 分隔符 + content，**content 逐字不变**；
- 写入/派生：ngram、trigram、embedding 三条派生路径都用同一索引文本；
- 版本：空库写当前版本；版本落后且有记录 → 启动后台重建；/health 可见；
- 幂等：重建两次派生结果逐字节一致；
- 失败：重建失败显式 ERROR + meta failed + /health 告警。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import textnorm
from app.config import load_config
from app.db import Database
from app.derive import DerivePipeline
from app.health import health_report
from app.index_text import (
    INDEX_TEXT_REBUILD_KEY,
    INDEX_TEXT_SEPARATOR,
    INDEX_TEXT_VERSION,
    INDEX_TEXT_VERSION_KEY,
    date_prefix,
    index_text_for,
)
from app.main import _run_index_text_rebuild, create_app
from app.rebuild_gate import RebuildGate
from app.retrieval import RetrievalService
from app.schema import (
    get_index_text_version,
    get_meta,
    index_text_rebuild_needed,
    init_db,
    set_meta,
)
from app.write_path import create_memory
from tests.conftest import prime_health
from tests.fake_embedding import HashingEmbeddingProvider
from tests.retrieval_helpers import add_memory, make_service


# ------------------------------------------------------------------ 前缀纯函数
@pytest.mark.parametrize(
    ("event_time", "precision", "expected"),
    [
        ("2025", "year", "2025 2025年"),
        ("2025-03", "month", "2025-03 2025年3月"),
        ("2025-03-14", "day", "2025-03-14 2025年3月14日"),
        ("2025-03-14", "instant", "2025-03-14 2025年3月14日"),
        # precision 缺失时按形状推断（兼容旧数据/直插测试行）
        ("2025", None, "2025 2025年"),
        ("2025-03", None, "2025-03 2025年3月"),
        ("2025-03-14", None, "2025-03-14 2025年3月14日"),
    ],
)
def test_event_time_prefix_exact(event_time, precision, expected):
    assert date_prefix(event_time, precision, 1_700_000_000_000) == expected


def test_recorded_at_utc_fallback():
    millis = int(datetime(2026, 11, 12, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    assert date_prefix(None, None, millis) == "2026-11-12 2026年11月12日"
    # 用 UTC 而非本地时区：同一毫秒在 UTC 的日期是确定的
    millis = int(datetime(2025, 12, 31, 23, 59, tzinfo=timezone.utc).timestamp() * 1000)
    assert date_prefix(None, None, millis) == "2025-12-31 2025年12月31日"


def test_empty_dates_have_no_prefix():
    assert date_prefix(None, None, None) == ""
    assert date_prefix("", None, None) == ""
    assert (
        index_text_for({"content": "正文", "event_time": None, "recorded_at": None})
        == "正文"
    )


def test_invalid_event_time_raises_explicitly():
    with pytest.raises(ValueError, match="月份非法"):
        date_prefix("2025-13", "month", None)
    with pytest.raises(ValueError, match="不是合法日期"):
        date_prefix("2025-02-30", "day", None)
    with pytest.raises(ValueError, match="不匹配"):
        date_prefix("2025", "month", None)
    with pytest.raises(ValueError, match="格式非法"):
        date_prefix("2025/03/14", "day", None)


# ------------------------------------------------------------------ 索引文本
def test_index_text_keeps_content_verbatim_and_has_separator():
    content = "体检查出血脂偏高，医生让我少吃油腻。"
    memory = {
        "content": content,
        "event_time": "2025-03-14",
        "event_time_precision": "day",
        "recorded_at": 1_700_000_000_000,
    }
    indexed = index_text_for(memory)
    prefix = "2025-03-14 2025年3月14日"
    assert indexed == prefix + INDEX_TEXT_SEPARATOR + content
    assert content in indexed
    # content 从分隔符后原样开始（没有被改写/去空白）
    assert indexed.split(INDEX_TEXT_SEPARATOR, 1)[1] == content
    # 纯函数：同一记录重复调用逐字节一致
    assert index_text_for(dict(memory)) == indexed
    # 分隔符不产 token：日期尾巴不会和正文首字粘成 CJK 双字
    assert "年体" not in textnorm.ngram_text(indexed)
    assert "2025" in textnorm.ngram_text(indexed).split()


def test_index_text_no_date_returns_content_only():
    content = "没有日期的记录"
    assert (
        index_text_for(
            {
                "content": content,
                "event_time": None,
                "event_time_precision": None,
                "recorded_at": None,
            }
        )
        == content
    )


# ------------------------------------------------------------------ 写入/派生路径
def test_write_path_indexes_prefix_but_content_unchanged(db):
    content = "体检查出血脂偏高，医生让我少吃油腻。"
    memory_id = create_memory(
        db,
        client_write_id="cw-index-text-write",
        content=content,
        session_id="s-index",
        source_client="t",
        event_time="2025",
        event_time_precision="year",
    )["id"]

    row = db.read_conn.execute(
        "SELECT content FROM memories WHERE id=?", (memory_id,)
    ).fetchone()
    assert row["content"] == content  # 事实层原文逐字不变

    ngrams = db.read_conn.execute(
        "SELECT ngram_text FROM memories_ngrams WHERE memory_id=?", (memory_id,)
    ).fetchone()[0]
    assert ngrams.startswith("2025 2025")  # ISO + 中文年份都进了 BM25 列
    assert "年体" not in ngrams

    trigram = db.read_conn.execute(
        "SELECT f.content AS indexed FROM memories_fts_trigram f "
        "JOIN memories m ON m.gseq=f.rowid WHERE m.id=?",
        (memory_id,),
    ).fetchone()[0]
    assert trigram.startswith("2025 2025年")

    # 「2025」只存在于日期字段：加前缀后 bigram 路能命中
    service = make_service(db, routes=("bigram",))
    outcome = service.bigram_route("2025 年体检查出了什么问题?", 10)
    assert memory_id in outcome.raw_ids
    result = service.search("2025 年体检查出了什么问题?")
    hit = next(item for item in result["items"] if item["id"] == memory_id)
    assert hit["content"] == content  # 输出给模型的 content 不变


def test_embedding_processor_uses_index_text(db):
    content = "复查了，血脂已经正常。"
    memory_id = create_memory(
        db,
        client_write_id="cw-index-text-embed",
        content=content,
        session_id="s-embed",
        source_client="t",
        event_time="2026-03-20",
        event_time_precision="day",
    )["id"]
    expected = index_text_for(
        dict(
            db.read_conn.execute(
                "SELECT content, event_time, event_time_precision, recorded_at "
                "FROM memories WHERE id=?",
                (memory_id,),
            ).fetchone()
        )
    )

    class RecordingProvider:
        model_id = "recording-test"

        def __init__(self) -> None:
            self.texts: list[str] = []

        def embed(self, texts):
            self.texts.extend(texts)
            return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    provider = RecordingProvider()
    pipeline = DerivePipeline(db, embedding_provider=provider)
    pipeline.tick()
    assert expected in provider.texts
    assert provider.texts[0] == expected
    assert expected.startswith("2026-03-20 2026年3月20日" + INDEX_TEXT_SEPARATOR)


def test_fts_processor_uses_index_text(db):
    content = "今年体检尿酸偏高，又开始忌口。"
    memory_id = create_memory(
        db,
        client_write_id="cw-index-text-fts",
        content=content,
        session_id="s-fts",
        source_client="t",
        event_time="2026-11-05",
        event_time_precision="day",
    )["id"]
    # 清掉写入路径的派生行，只让 fts processor 重建，验证 processor 本身用索引文本
    with db.write_txn() as conn:
        conn.execute("DELETE FROM memories_ngrams WHERE memory_id=?", (memory_id,))
        conn.execute("DELETE FROM derive_state WHERE memory_id=?", (memory_id,))
    pipeline = DerivePipeline(db, stub_success=True)
    pipeline._run_stage("fts")
    ngrams = db.read_conn.execute(
        "SELECT ngram_text FROM memories_ngrams WHERE memory_id=?", (memory_id,)
    ).fetchone()[0]
    assert ngrams.startswith("2026-11-05 2026")


# ------------------------------------------------------------------ 版本/重建
def test_empty_db_is_stamped_with_current_version(db):
    assert get_index_text_version(db) == INDEX_TEXT_VERSION
    assert index_text_rebuild_needed(db) is False


def test_rebuild_all_is_idempotent_with_index_text(db):
    create_memory(
        db,
        client_write_id="cw-idem-1",
        content="体检查出血脂偏高，医生让我少吃油腻。",
        session_id="s-idem",
        source_client="t",
        event_time="2025",
        event_time_precision="year",
    )
    create_memory(
        db,
        client_write_id="cw-idem-2",
        content="复查了，血脂已经正常。",
        session_id="s-idem",
        source_client="t",
        event_time="2026-03-20",
        event_time_precision="day",
    )
    provider = HashingEmbeddingProvider(dim=8)
    pipeline = DerivePipeline(db, embedding_provider=provider)

    def snapshot() -> dict:
        conn = db.read_conn
        return {
            "ngrams": [
                tuple(row)
                for row in conn.execute(
                    "SELECT memory_id, ngram_text FROM memories_ngrams ORDER BY memory_id"
                ).fetchall()
            ],
            "trigram": [
                tuple(row)
                for row in conn.execute(
                    "SELECT rowid, content FROM memories_fts_trigram ORDER BY rowid"
                ).fetchall()
            ],
            "embeddings": [
                tuple(row)
                for row in conn.execute(
                    "SELECT memory_id, hex(vector), model_id, dim FROM embeddings "
                    "ORDER BY memory_id"
                ).fetchall()
            ],
            "version": get_meta(db, INDEX_TEXT_VERSION_KEY),
        }

    pipeline.rebuild_all()
    first = snapshot()
    pipeline.rebuild_all()
    second = snapshot()
    assert first == second
    assert first["version"] == str(INDEX_TEXT_VERSION)
    assert all(row[1].startswith("2025 2025") for row in first["ngrams"] if "2025" in row[1])


def _make_stale_db(path, *, content="体检查出血脂偏高，医生让我少吃油腻。"):
    """建一个「版本落后且派生列是 v1 内容」的真实库。"""
    db = Database(path)
    init_db(db)
    memory_id = create_memory(
        db,
        client_write_id="cw-stale",
        content=content,
        session_id="s-stale",
        source_client="t",
        event_time="2025",
        event_time_precision="year",
    )["id"]
    # 模拟旧版本派生：清掉日期前缀，只留 content-only ngram；meta 版本改成 1
    with db.write_txn() as conn:
        conn.execute(
            "UPDATE memories_ngrams SET ngram_text=? WHERE memory_id=?",
            (textnorm.ngram_text(content), memory_id),
        )
        rowid = conn.execute(
            "SELECT gseq FROM memories WHERE id=?", (memory_id,)
        ).fetchone()[0]
        conn.execute("DELETE FROM memories_fts_bigram WHERE rowid=?", (rowid,))
        conn.execute(
            "INSERT INTO memories_fts_bigram(rowid, ngram_text) VALUES (?, ?)",
            (rowid, textnorm.ngram_text(content)),
        )
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (INDEX_TEXT_VERSION_KEY, "1"),
        )
    db.close()
    return memory_id


def _clear_embedding_env(monkeypatch):
    for name in (
        "EMBEDDING_MODEL",
        "DSH_EMBEDDING_MODEL",
        "EMBEDDING_BASE_URL",
        "DSH_EMBEDDING_BASE_URL",
        "DSH_EMBEDDING_API_URL",
        "EMBEDDING_API_KEY",
        "DSH_EMBEDDING_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DSH_DERIVE_LOOP_ENABLED", "0")
    monkeypatch.setenv("DSH_BACKUP_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("RETRIEVAL_ROUTES", "bigram")


def test_version_mismatch_triggers_background_rebuild_at_startup(
    tmp_path, monkeypatch
):
    path = str(tmp_path / "stale.db")
    memory_id = _make_stale_db(path)
    _clear_embedding_env(monkeypatch)

    app = create_app(db_path=path)
    with TestClient(app) as client:
        thread = client.app.state.index_text_rebuild_thread
        assert thread is not None, "版本落后且有记录时必须触发一次后台重建"
        thread.join(timeout=15)
        assert not thread.is_alive()

        health = prime_health(client)
        state = health["index_text_version"]
        assert state["current"] == INDEX_TEXT_VERSION
        assert state["stored"] == INDEX_TEXT_VERSION
        assert state["up_to_date"] is True
        assert state["rebuild_needed"] is False
        assert state["last_rebuild"]["status"] == "ok"

        ngrams = app.state.db.read_conn.execute(
            "SELECT ngram_text FROM memories_ngrams WHERE memory_id=?", (memory_id,)
        ).fetchone()[0]
        assert ngrams.startswith("2025 2025")  # 派生列已按 v2 重建
        assert "年体" not in ngrams
        # 事实层 content 不变
        assert (
            app.state.db.read_conn.execute(
                "SELECT content FROM memories WHERE id=?", (memory_id,)
            ).fetchone()[0]
            == "体检查出血脂偏高，医生让我少吃油腻。"
        )


def test_startup_does_not_rebuild_when_version_current(tmp_path, monkeypatch):
    path = str(tmp_path / "current.db")
    db = Database(path)
    init_db(db)
    create_memory(
        db,
        client_write_id="cw-current",
        content="当前版本记录",
        session_id="s-current",
        source_client="t",
    )
    assert get_index_text_version(db) == INDEX_TEXT_VERSION
    db.close()
    _clear_embedding_env(monkeypatch)

    app = create_app(db_path=path)
    with TestClient(app) as client:
        assert client.app.state.index_text_rebuild_thread is None
        assert client.app.state.index_text_needs_rebuild is False


def test_index_text_version_visible_in_health(client):
    health = prime_health(client)
    state = health["index_text_version"]
    assert state["current"] == INDEX_TEXT_VERSION
    assert state["stored"] == INDEX_TEXT_VERSION
    assert state["up_to_date"] is True
    assert state["last_rebuild"] is None


def test_partial_rebuild_does_not_claim_version_with_stale_vectors(db):
    """skip_embedding 保留旧向量时不能宣称 v2：否则旧 content-only 向量冒充新索引。"""
    add_memory(db, "有旧向量的记录", recorded_at=1_700_000_000_000)
    provider = HashingEmbeddingProvider(dim=8)
    DerivePipeline(db, embedding_provider=provider).rebuild_all()
    assert db.read_conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == 1
    set_meta(db, INDEX_TEXT_VERSION_KEY, "1")

    no_provider = DerivePipeline(db, stub_success=True)
    counts = no_provider.rebuild_all(skip_embedding=True)
    assert counts["fts"] == 1
    # 旧向量保留，版本保持 1（不冒充 v2）
    assert db.read_conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == 1
    assert get_index_text_version(db) == 1
    assert index_text_rebuild_needed(db) is True

    # 无 provider 的完整重建会清掉旧向量并声明 v2（后续 tick 可再补向量）
    no_provider.rebuild_all()
    assert db.read_conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == 0
    assert get_index_text_version(db) == INDEX_TEXT_VERSION
    assert index_text_rebuild_needed(db) is False


def test_rebuild_failure_is_explicit_and_exposed_in_health(db, caplog):
    memory_id = add_memory(db, "失败测试记录", recorded_at=1_700_000_000_000)
    assert memory_id
    set_meta(db, INDEX_TEXT_VERSION_KEY, "1")
    pipeline = DerivePipeline(db, stub_success=True)
    retrieval = RetrievalService(db, load_config())
    gate = RebuildGate()

    def boom(**kwargs):
        raise RuntimeError("simulated index text rebuild failure")

    pipeline.rebuild_all = boom  # type: ignore[method-assign]
    caplog.set_level(logging.ERROR, logger="factlayer.main")
    _run_index_text_rebuild(db, pipeline, retrieval, gate, 1)

    assert gate.is_rebuilding is False
    detail = json.loads(get_meta(db, INDEX_TEXT_REBUILD_KEY))
    assert detail["status"] == "failed"
    assert "simulated index text rebuild failure" in detail["error"]
    assert any(
        rec.levelno == logging.ERROR and "索引文本版本重建失败" in rec.getMessage()
        for rec in caplog.records
    )
    health = health_report(db, DerivePipeline(db, stub_success=True))
    assert health["index_text_version"]["rebuild_needed"] is True
    assert "index_text_rebuild_failed" in health["alerts"]
    assert any(
        alert.startswith("index_text_version_stale") for alert in health["alerts"]
    )
