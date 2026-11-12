"""派生循环默认值（2026-11-12 联调实测 P0）。

``DSH_DERIVE_LOOP_ENABLED`` 曾经默认 0：写入后 embedding 永远 pending，默认
向量路线永久降级。新语义：``RETRIEVAL_ROUTES`` 含 vector 且 embedding 已配置
→ 默认开；显式 ``DSH_DERIVE_LOOP_ENABLED=0`` 才关。
"""

from __future__ import annotations

import logging
import threading

from fastapi.testclient import TestClient

from app.config import load_config
from app.main import create_app
from tests.conftest import prime_health


def _vector_env(monkeypatch) -> None:
    monkeypatch.setenv("RETRIEVAL_ROUTES", "vector")
    monkeypatch.setenv("EMBEDDING_MODEL", "test-embedding")
    monkeypatch.setenv("EMBEDDING_BASE_URL", "http://127.0.0.1:1/v1")
    monkeypatch.setenv("DSH_BACKUP_INTERVAL_SECONDS", "0")


def test_derive_loop_defaults_on_for_vector_plus_embedding(monkeypatch):
    _vector_env(monkeypatch)
    monkeypatch.delenv("DSH_DERIVE_LOOP_ENABLED", raising=False)
    assert load_config().derive_loop_enabled is True


def test_derive_loop_explicit_zero_wins(monkeypatch):
    _vector_env(monkeypatch)
    monkeypatch.setenv("DSH_DERIVE_LOOP_ENABLED", "0")
    assert load_config().derive_loop_enabled is False


def test_derive_loop_stays_off_without_embedding(monkeypatch):
    _vector_env(monkeypatch)
    monkeypatch.delenv("DSH_DERIVE_LOOP_ENABLED", raising=False)
    for name in (
        "EMBEDDING_MODEL",
        "DSH_EMBEDDING_MODEL",
        "EMBEDDING_BASE_URL",
        "DSH_EMBEDDING_BASE_URL",
        "DSH_EMBEDDING_API_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    assert load_config().derive_loop_enabled is False


def test_lifespan_starts_derive_loop_for_vector_plus_embedding(
    tmp_path, monkeypatch, caplog
):
    _vector_env(monkeypatch)
    monkeypatch.delenv("DSH_DERIVE_LOOP_ENABLED", raising=False)
    app = create_app(db_path=str(tmp_path / "loop-on.db"))
    with caplog.at_level(logging.INFO, logger="factlayer.main"):
        with TestClient(app):
            assert app.state.derive_loop_enabled is True
            thread = app.state.derive_loop_thread
            assert thread is not None
            assert thread.name == "derive-backup-loop"
            assert thread.is_alive()
    assert "deriveloop started (routes=vector)" in caplog.text


def test_lifespan_skips_background_loop_when_everything_disabled(
    tmp_path, monkeypatch, caplog
):
    _vector_env(monkeypatch)
    monkeypatch.setenv("DSH_DERIVE_LOOP_ENABLED", "0")
    # checkpoint/optimize 默认是开的；断言「线程不启动」必须把它们也显式关掉，
    # 否则线程会因为维护任务而启动（这正是审查 #2 要修的静默全灭反例）。
    monkeypatch.setenv("DSH_WAL_CHECKPOINT_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("DSH_FTS_OPTIMIZE_INTERVAL_SECONDS", "0")
    app = create_app(db_path=str(tmp_path / "loop-off.db"))
    with caplog.at_level(logging.INFO, logger="factlayer.main"):
        with TestClient(app) as client:
            assert app.state.derive_loop_enabled is False
            assert app.state.derive_loop_thread is None
            health = prime_health(client)
            assert health["maintenance"]["loop_enabled"] is False
            assert health["maintenance"]["checkpoint"]["enabled"] is False
            assert health["maintenance"]["fts_optimize"]["enabled"] is False
    assert "维护线程未启动" in caplog.text
    assert "checkpoint/optimize 均关闭" in caplog.text


def test_lifespan_starts_background_loop_for_checkpoint_only(
    tmp_path, monkeypatch, caplog
):
    """derive=0 + backup=0 + checkpoint>0 的合法部署：线程必须启动（审查 #2）。"""
    monkeypatch.setenv("RETRIEVAL_ROUTES", "bigram")
    monkeypatch.setenv("DSH_DERIVE_LOOP_ENABLED", "0")
    monkeypatch.setenv("DSH_BACKUP_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("DSH_WAL_AUTOCHECKPOINT_PAGES", "1000")
    monkeypatch.setenv("DSH_WAL_CHECKPOINT_INTERVAL_SECONDS", "300")
    monkeypatch.setenv("DSH_WAL_CHECKPOINT_IDLE_SECONDS", "60")
    monkeypatch.setenv("DSH_FTS_OPTIMIZE_INTERVAL_SECONDS", "0")
    app = create_app(db_path=str(tmp_path / "loop-checkpoint.db"))
    with caplog.at_level(logging.INFO, logger="factlayer.main"):
        with TestClient(app) as client:
            assert app.state.derive_loop_enabled is False
            thread = app.state.derive_loop_thread
            assert thread is not None and thread.is_alive()
            health = prime_health(client)
            assert health["maintenance"]["loop_enabled"] is True
            assert health["maintenance"]["checkpoint"]["enabled"] is True
            assert health["maintenance"]["checkpoint"]["interval_seconds"] == 300
            assert health["maintenance"]["fts_optimize"]["enabled"] is False
    assert "background loop started" in caplog.text


def test_background_loop_closes_its_thread_read_conn(tmp_path, monkeypatch):
    """后台线程退出时必须关闭自己的 thread-local 读连接（审查 #3）。"""
    monkeypatch.setenv("RETRIEVAL_ROUTES", "bigram")
    monkeypatch.setenv("DSH_DERIVE_LOOP_ENABLED", "0")
    monkeypatch.setenv("DSH_BACKUP_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("DSH_WAL_CHECKPOINT_INTERVAL_SECONDS", "300")
    monkeypatch.setenv("DSH_FTS_OPTIMIZE_INTERVAL_SECONDS", "0")
    app = create_app(db_path=str(tmp_path / "loop-close.db"))
    calls: list[str] = []
    original = app.state.db.close_thread_read_conn

    def recording() -> None:
        calls.append(threading.current_thread().name)
        original()

    monkeypatch.setattr(app.state.db, "close_thread_read_conn", recording)
    with TestClient(app):
        thread = app.state.derive_loop_thread
        assert thread is not None and thread.is_alive()
    assert "derive-backup-loop" in calls
