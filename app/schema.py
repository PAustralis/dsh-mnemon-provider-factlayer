"""全部建表 SQL + FTS5 虚表与触发器 + 迁移入口。

依据 docs/01-事实层设计.md（定稿 v7 + 评审修订）：
- memories 只追加：用 RAISE(ABORT) 触发器在数据库层禁止 UPDATE/DELETE。
- supersedes 语义为「撤销作废也走追加」：同一 (from_id,to_id) 可有多条边，
  recorded_at 最新一条决定状态，故主键为 (from_id, to_id, recorded_at)。
  （设计文档表结构写 PRIMARY KEY (from_id,to_id)，与「追加 + 取最新边」自相矛盾，
  本实现按语义取三列主键，见报告偏差说明。）
- 派生侧（derive_state / embeddings / entities / FTS5）全部可弃、可清空重建。
"""

from __future__ import annotations

from .db import Database

SCHEMA_SQL = """
-- 事实表：只追加，永不 UPDATE/DELETE
CREATE TABLE IF NOT EXISTS memories (
  id           TEXT PRIMARY KEY,
  client_write_id TEXT NOT NULL UNIQUE,
  content      TEXT NOT NULL,
  recorded_at  INTEGER NOT NULL,
  session_id   TEXT NOT NULL,
  seq          INTEGER NOT NULL,
  event_time   TEXT,
  event_time_precision TEXT CHECK (event_time_precision IN ('year','month','day','instant')),
  source_client TEXT NOT NULL,
  CHECK ((event_time IS NULL) = (event_time_precision IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_memories_session ON memories(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_memories_time ON memories(recorded_at DESC);

-- 数据库层强制只追加：任何 UPDATE/DELETE 直接 ABORT
CREATE TRIGGER IF NOT EXISTS memories_no_update
BEFORE UPDATE ON memories
BEGIN
  SELECT RAISE(ABORT, 'memories is append-only: UPDATE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS memories_no_delete
BEFORE DELETE ON memories
BEGIN
  SELECT RAISE(ABORT, 'memories is append-only: DELETE forbidden');
END;

-- 作废边：追加式；最新边决定 (from_id,to_id) 状态
CREATE TABLE IF NOT EXISTS supersedes (
  from_id    TEXT NOT NULL,
  to_id      TEXT NOT NULL,
  kind       TEXT NOT NULL CHECK (kind IN ('supersede','revoke')),
  reason     TEXT,
  recorded_at INTEGER NOT NULL,
  source_client TEXT NOT NULL,
  PRIMARY KEY (from_id, to_id, recorded_at)
);
CREATE INDEX IF NOT EXISTS idx_supersedes_to ON supersedes(to_id, recorded_at DESC);

-- 派生侧水位表
CREATE TABLE IF NOT EXISTS derive_state (
  memory_id TEXT NOT NULL,
  stage     TEXT NOT NULL CHECK (stage IN ('embedding','entity','graph')),
  done_at   INTEGER NOT NULL,
  PRIMARY KEY (memory_id, stage)
);
CREATE TABLE IF NOT EXISTS embeddings (
  memory_id TEXT PRIMARY KEY,
  vector    BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS entities (
  memory_id TEXT NOT NULL,
  entity    TEXT NOT NULL,
  PRIMARY KEY (memory_id, entity)
);
CREATE INDEX IF NOT EXISTS idx_entities ON entities(entity);

-- FTS5 全文索引（外部内容表，触发器同步；派生侧，可弃可重建）
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
  content,
  content='memories',
  content_rowid='rowid'
);
CREATE TRIGGER IF NOT EXISTS memories_fts_ai AFTER INSERT ON memories BEGIN
  INSERT INTO memories_fts(rowid, content) VALUES (new.rowid, new.content);
END;
-- memories 已被上方 RAISE 触发器禁止 UPDATE/DELETE，以下两个同步触发器
-- 仅保持外部内容表 FTS5 的标准形态，正常路径不会触达。
CREATE TRIGGER IF NOT EXISTS memories_fts_ad AFTER DELETE ON memories BEGIN
  INSERT INTO memories_fts(memories_fts, rowid, content) VALUES('delete', old.rowid, old.content);
END;
CREATE TRIGGER IF NOT EXISTS memories_fts_au AFTER UPDATE ON memories BEGIN
  INSERT INTO memories_fts(memories_fts, rowid, content) VALUES('delete', old.rowid, old.content);
  INSERT INTO memories_fts(rowid, content) VALUES (new.rowid, new.content);
END;

-- 元信息表（如 last_backup_at）
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


def init_db(db: Database) -> None:
    """迁移入口：库不存在则建；已存在则按 IF NOT EXISTS 幂等补齐。"""
    db.execute_script(SCHEMA_SQL)


def get_meta(db: Database, key: str) -> str | None:
    row = db.read_conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(db: Database, key: str, value: str) -> None:
    with db.write_txn() as conn:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
