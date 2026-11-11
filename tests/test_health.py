"""/health 形态 + 备份：VACUUM INTO、last_backup_at、保留最近 N 份。"""

from app.backup import backup_dir_for, run_backup
from tests.conftest import write_one


def test_health_shape_and_last_backup_at(client, db, db_path):
    write_one(client, cwid="cw-h1", content="x", session_id="s1")
    health = client.get("/health").json()
    assert {
        "pending",
        "failures",
        "last_backup_at",
        "last_backup_error",
        "backup_consecutive_failures",
        "config",
        "retrieval",
    } <= set(health)
    assert set(health["pending"]) == {"fts", "embedding", "entity", "graph"}
    assert health["pending"]["embedding"] == 1
    assert health["failures"] == {"fts": 0, "embedding": 0, "entity": 0, "graph": 0}
    assert health["last_backup_at"] is None
    assert health["last_backup_error"] is None
    assert health["backup_consecutive_failures"] == 0
    # 生效配置快照：模型名 / 维度 / 启用路线 / 契约常量 / 开关
    assert health["config"]["embedding_model"] == ""
    assert health["config"]["embedding_dim"] == "auto"
    assert health["config"]["routes"] == ["bigram"]
    assert health["config"]["effective_routes"] == ["bigram"]
    assert health["config"]["final_k"] == 4
    assert health["config"]["item_max_chars"] == 1200
    assert health["config"]["total_max_chars"] == 4800
    assert health["config"]["score_semantics"] == "provider-native"
    assert health["config"]["expand_enabled"] is False
    assert set(health["retrieval"]["routes"]) == {
        "vector",
        "bigram",
        "trigram",
        "graph",
    }
    assert health["retrieval"]["routes"]["bigram"]["empty_rate"] is None
    assert health["retrieval"]["degraded"] is False
    # v8 §5/F：hidden 大小、熔断状态、金丝雀
    assert health["retrieval"]["hidden_size"] == 0
    assert health["retrieval"]["circuit"]["circuit_open"] is False
    assert health["retrieval"]["canary"]["status"] in {
        "insufficient_samples",
        "ok",
    }

    resp = client.post("/admin/backup")
    assert resp.status_code == 200
    health = client.get("/health").json()
    assert isinstance(health["last_backup_at"], int)
    assert health["last_backup_at"] > 0
    assert health["last_backup_error"] is None
    assert health["backup_consecutive_failures"] == 0
    assert any(backup_dir_for(db_path).glob("backup-*.db"))


def test_health_hidden_size_reflects_voided_with_live_head(client):
    a = write_one(client, cwid="cw-ha", content="旧说法", session_id="s1").json()
    b = write_one(client, cwid="cw-hb", content="新说法", session_id="s2").json()
    edge = client.post(
        "/supersedes",
        json={
            "from_id": b["id"],
            "to_id": a["id"],
            "kind": "supersede",
            "source_client": "t",
        },
    )
    assert edge.status_code == 200
    health = client.get("/health").json()
    assert health["retrieval"]["hidden_size"] == 1


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
