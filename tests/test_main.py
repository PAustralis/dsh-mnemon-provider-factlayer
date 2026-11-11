"""工厂模式（import 无副作用）+ SQLite 异常不泄露原文。"""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_import_app_main_creates_no_facts_db(tmp_path):
    env = os.environ.copy()
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        str(REPO_ROOT) + (os.pathsep + existing if existing else "")
    )
    proc = subprocess.run(
        [sys.executable, "-c", "import app.main"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert not (tmp_path / "facts.db").exists(), "import app.main 不得建库"
    assert not (tmp_path / "backups").exists()


def test_module_has_no_eager_app_instance():
    import app.main as main_mod

    assert not hasattr(main_mod, "app"), "模块级不得提前创建 FastAPI 实例"
    assert callable(main_mod.create_app)


def test_sqlite_error_detail_not_leaked(client, monkeypatch):
    import app.main as main_mod

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error: /srv/secret/facts.db")

    monkeypatch.setattr(main_mod, "create_memory", boom)
    resp = client.post(
        "/memories",
        json={
            "client_write_id": "cw-boom",
            "content": "x",
            "session_id": "s-boom",
            "source_client": "t",
        },
    )
    assert resp.status_code == 500
    assert "disk I/O" not in resp.text
    assert "/srv/secret" not in resp.text
    assert "内部存储错误" in resp.json()["detail"]
