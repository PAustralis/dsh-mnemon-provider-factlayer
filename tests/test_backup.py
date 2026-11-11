"""备份原子性 + 自动调度（间隔 / 关闭 / 失败状态 / 后台线程接线）。"""

import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.backup import BackupScheduler, backup_dir_for, run_backup
from app.health import last_backup_at


# ---- 原子替换 ----------------------------------------------------------------


def test_vacuum_failure_keeps_old_backup_and_cleans_tmp(db, monkeypatch):
    bdir = backup_dir_for(db.path)
    first = Path(run_backup(db, keep=7))
    old_bytes = first.read_bytes()

    def failing_vacuum(dest: str) -> None:
        Path(dest).write_bytes(b"partial-temp")  # 模拟写了一半
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(db, "vacuum_into", failing_vacuum)
    with pytest.raises(sqlite3.OperationalError):
        run_backup(db, keep=7)

    assert first.exists(), "VACUUM 失败不得删除旧备份"
    assert first.read_bytes() == old_bytes, "旧备份内容必须原样保留"
    assert not list(bdir.glob(".tmp-*")), "失败后临时文件必须清理"
    assert len(list(bdir.glob("backup-*.db"))) == 1


def test_successful_rerun_atomically_replaces_and_leaves_no_tmp(db):
    bdir = backup_dir_for(db.path)
    first = run_backup(db, keep=7)
    from app.write_path import create_memory

    create_memory(
        db, client_write_id="cw-atomic", content="new-data",
        session_id="sa", source_client="t",
    )
    second = run_backup(db, keep=7)
    assert second == first  # 同日同路径
    assert not list(bdir.glob(".tmp-*"))

    conn = sqlite3.connect(second)
    try:
        assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
    finally:
        conn.close()


# ---- 调度 --------------------------------------------------------------------


def test_scheduler_backs_up_when_due(db):
    scheduler = BackupScheduler(db, interval_seconds=3600)
    dest = scheduler.maybe_backup()
    assert dest is not None and Path(dest).exists()
    assert scheduler.maybe_backup() is None  # 未到期

    last = last_backup_at(db)
    assert last is not None
    # 到期后（用注入 now 避免真等一小时）
    assert scheduler.maybe_backup(now=last + 3600 * 1000 + 1) is not None


def test_scheduler_interval_zero_disables_backups(db):
    scheduler = BackupScheduler(db, interval_seconds=0)
    assert scheduler.maybe_backup() is None
    assert not backup_dir_for(db.path).exists()


def test_scheduler_records_consecutive_failures_and_resets_on_success(db, monkeypatch):
    import app.backup as backup_mod

    original = backup_mod.run_backup

    def boom(_db, keep=7):
        raise OSError("simulated disk full")

    monkeypatch.setattr(backup_mod, "run_backup", boom)
    scheduler = BackupScheduler(db, interval_seconds=0)

    for expected in (1, 2):
        with pytest.raises(OSError):
            scheduler.run_now()
        state = scheduler.status()
        assert state["backup_consecutive_failures"] == expected
        assert "OSError" in state["last_backup_error"]
        assert "simulated disk full" in state["last_backup_error"]

    monkeypatch.setattr(backup_mod, "run_backup", original)
    scheduler.run_now()
    state = scheduler.status()
    assert state["backup_consecutive_failures"] == 0
    assert state["last_backup_error"] is None


# ---- /health + 后台线程接线 --------------------------------------------------


def test_manual_backup_failure_exposed_in_health(client, monkeypatch):
    import app.backup as backup_mod

    original = backup_mod.run_backup

    def boom(_db, keep=7):
        raise sqlite3.OperationalError("disk I/O error: /secret/facts.db")

    monkeypatch.setattr(backup_mod, "run_backup", boom)
    resp = client.post("/admin/backup")
    assert resp.status_code == 500
    assert "disk I/O" not in resp.text and "secret" not in resp.text
    assert "备份失败" in resp.json()["detail"]

    health = client.get("/health").json()
    assert health["backup_consecutive_failures"] == 1
    assert "OperationalError" in health["last_backup_error"]

    # 恢复后手动备份成功，健康状态清零
    monkeypatch.setattr(backup_mod, "run_backup", original)
    assert client.post("/admin/backup").status_code == 200
    health = client.get("/health").json()
    assert health["backup_consecutive_failures"] == 0
    assert health["last_backup_error"] is None


def test_background_thread_runs_due_backup(tmp_path, monkeypatch):
    import app.main as main_mod

    monkeypatch.setattr(main_mod, "DERIVE_LOOP_INTERVAL_SECONDS", 0.05)
    monkeypatch.setenv("DSH_DERIVE_LOOP_ENABLED", "0")
    monkeypatch.setenv("DSH_BACKUP_INTERVAL_SECONDS", "3600")

    db_path = str(tmp_path / "facts.db")
    app = main_mod.create_app(db_path=db_path)
    bdir = backup_dir_for(db_path)
    with TestClient(app) as c:
        deadline = time.time() + 5
        last = None
        while time.time() < deadline:
            last = c.get("/health").json()["last_backup_at"]
            if last is not None and list(bdir.glob("backup-*.db")):
                break
            time.sleep(0.02)
        assert last is not None, "后台线程应自动完成首次备份并写入 last_backup_at"
    assert list(bdir.glob("backup-*.db"))
