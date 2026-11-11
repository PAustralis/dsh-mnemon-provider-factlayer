"""/health 形态 + 备份：VACUUM INTO、last_backup_at、保留最近 N 份。"""

from app.backup import backup_dir_for, run_backup
from tests.conftest import write_one


def test_health_shape_and_last_backup_at(client, db, db_path):
    write_one(client, cwid="cw-h1", content="x", session_id="s1")
    health = client.get("/health").json()
    assert set(health) == {
        "pending",
        "failures",
        "last_backup_at",
        "last_backup_error",
        "backup_consecutive_failures",
    }
    assert set(health["pending"]) == {"embedding", "entity", "graph"}
    assert health["pending"]["embedding"] == 1
    assert health["failures"] == {"embedding": 0, "entity": 0, "graph": 0}
    assert health["last_backup_at"] is None
    assert health["last_backup_error"] is None
    assert health["backup_consecutive_failures"] == 0

    resp = client.post("/admin/backup")
    assert resp.status_code == 200
    health = client.get("/health").json()
    assert isinstance(health["last_backup_at"], int)
    assert health["last_backup_at"] > 0
    assert health["last_backup_error"] is None
    assert health["backup_consecutive_failures"] == 0
    assert any(backup_dir_for(db_path).glob("backup-*.db"))


def test_backup_creates_file_and_meta(db, db_path):
    dest = run_backup(db, keep=7)
    import pathlib

    p = pathlib.Path(dest)
    assert p.exists() and p.parent.name == "backups"
    assert p.name.startswith("backup-")
    row = db.read_conn.execute(
        "SELECT value FROM meta WHERE key='last_backup_at'"
    ).fetchone()
    assert row is not None


def test_backup_rerun_same_day_overwrites(db, db_path):
    run_backup(db, keep=7)
    run_backup(db, keep=7)  # 同日重跑：删旧再 VACUUM INTO，不报错
    files = list(backup_dir_for(db_path).glob("backup-*.db"))
    assert len(files) == 1


def test_backup_prune_keeps_last_n(db, db_path):
    bdir = backup_dir_for(db_path)
    bdir.mkdir(parents=True, exist_ok=True)
    for day in ("20260101", "20260102", "20260103", "20260104"):
        (bdir / f"backup-{day}.db").write_bytes(b"old")
    run_backup(db, keep=3)
    remaining = sorted(p.name for p in bdir.glob("backup-*.db"))
    assert len(remaining) == 3
    assert "backup-20260101.db" not in remaining  # 最旧的被清


def test_backup_contains_data(db, db_path, client):
    from app.write_path import create_memory

    create_memory(
        db, client_write_id="cw-bk", content="backup-me",
        session_id="sbk", source_client="t",
    )
    dest = run_backup(db, keep=7)
    import sqlite3

    conn = sqlite3.connect(dest)
    try:
        n = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    finally:
        conn.close()
    assert n == 1
