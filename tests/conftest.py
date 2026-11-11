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
def client(db_path):
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
