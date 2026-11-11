"""派生循环默认值（2026-11-12 联调实测 P0）。

``DSH_DERIVE_LOOP_ENABLED`` 曾经默认 0：写入后 embedding 永远 pending，默认
向量路线永久降级。新语义：``RETRIEVAL_ROUTES`` 含 vector 且 embedding 已配置
→ 默认开；显式 ``DSH_DERIVE_LOOP_ENABLED=0`` 才关。
"""

from __future__ import annotations

import logging

from fastapi.testclient import TestClient

from app.config import load_config
from app.main import create_app


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


def test_lifespan_skips_derive_loop_when_explicitly_disabled(tmp_path, monkeypatch):
    _vector_env(monkeypatch)
    monkeypatch.setenv("DSH_DERIVE_LOOP_ENABLED", "0")
    app = create_app(db_path=str(tmp_path / "loop-off.db"))
    with TestClient(app):
        assert app.state.derive_loop_enabled is False
        assert app.state.derive_loop_thread is None
