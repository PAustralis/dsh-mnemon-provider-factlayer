import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import Database  # noqa: E402
from app.derive import DerivePipeline  # noqa: E402
from app.main import create_app  # noqa: E402
from app.schema import init_db  # noqa: E402


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "facts.db")


@pytest.fixture
def db(db_path):
    database = Database(db_path)
    init_db(database)
    yield database
    database.close()


@pytest.fixture
def client(db_path, monkeypatch):
    # 测试里关闭后台线程，避免自动备份与 /health 断言互相干扰；
    # 调度逻辑在 tests/test_backup.py 里单独测。
    # 检索层默认只开 bigram：vector 路线需要 EMBEDDING_MODEL / 网络，
    # 测试统一固定 bigram，向量相关测试自己注入假 provider。
    monkeypatch.setenv("DSH_DERIVE_LOOP_ENABLED", "0")
    monkeypatch.setenv("DSH_BACKUP_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("RETRIEVAL_ROUTES", "bigram")
    # 本地 .env 可能配置真实 embedding：测试保持 hermetic，清掉这些变量，
    # 向量相关测试一律注入假 provider（避免联网、也让 /health 快照稳定）。
    for _name in (
        "EMBEDDING_MODEL",
        "DSH_EMBEDDING_MODEL",
        "EMBEDDING_BASE_URL",
        "DSH_EMBEDDING_BASE_URL",
        "DSH_EMBEDDING_API_URL",
        "EMBEDDING_API_KEY",
        "DSH_EMBEDDING_API_KEY",
    ):
        monkeypatch.delenv(_name, raising=False)
    app = create_app(db_path=db_path)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def pipeline(db):
    return DerivePipeline(db, stub_success=True)


def write_one(client, *, session_id="s1", content="hello", cwid=None, **kw):
    payload = {
        "client_write_id": cwid or f"cwid-{kw.get('salt', session_id)}-{kw.get('i', 0)}",
        "content": content,
        "session_id": session_id,
        "source_client": "test-client",
    }
    payload.update(kw)
    for key in ("salt", "i"):
        payload.pop(key, None)
    return client.post("/memories", json=payload)
