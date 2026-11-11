"""全部建表 SQL + FTS5 虚表与触发器 + 迁移入口。

依据 docs/01-事实层设计.md（定稿 v7 + 评审修订）：
- memories 只追加：用 RAISE(ABORT) 触发器在数据库层禁止 UPDATE/DELETE。
- supersedes 语义为「撤销作废也走追加」：同一 (from_id,to_id) 可有多条边，
  recorded_at 最新一条决定状态。主键为自增 ``id``（追加顺序），
  (from_id,to_id,recorded_at) 仅为普通索引——同毫秒重复 revoke/supersede 不再撞 PK，
  同毫秒时用 ``id`` 做 tie-break。
- 派生侧（derive_state / embeddings / entities / FTS5）全部可弃、可清空重建。

迁移：本层早期版本把 (from_id,to_id,recorded_at) 当主键。init_db() 会检测旧结构，
自动重建 supersedes 表并保留全部边记录（项目尚无生产数据，重建是安全做法）。
"""

from __future__ import annotations

import logging

from .db import Database

logger = logging.getLogger("factlayer.schema")

SUPERSEDES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS supersedes (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,  -- 追加顺序；同毫秒时用于 tie-break
  from_id    TEXT NOT NULL,               -- 裁决记录 id
  to_id      TEXT NOT NULL,               -- 被作用记录 id
  kind       TEXT NOT NULL CHECK (kind IN ('supersede','revoke')),
  reason     TEXT,
  recorded_at INTEGER NOT NULL,
  source_client TEXT NOT NULL
);
"""

DERIVE_STATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS derive_state (
  memory_id TEXT NOT NULL,
  stage     TEXT NOT NULL CHECK (stage IN ('fts','embedding','entity','graph')),
  done_at   INTEGER NOT NULL,
  PRIMARY KEY (memory_id, stage)
);
"""

SCHEMA_SQL = f"""
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
{SUPERSEDES_TABLE_SQL}
-- 每个 pair 取最新边的索引（recorded_at 同毫秒时按 id 决胜）
CREATE INDEX IF NOT EXISTS idx_supersedes_pair
  ON supersedes(from_id, to_id, recorded_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_supersedes_to ON supersedes(to_id, recorded_at DESC);

-- 派生侧水位表（stage 含检索层的 fts：生成 ngrams + 写 FTS5，见 docs/02 §3.1）
{DERIVE_STATE_TABLE_SQL}
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

-- 元信息表（last_backup_at / embedding_model / embedding_dim / retrieval:eval）
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- ---- 检索层派生结构（全部可弃、可重建；不在 memories 上加列） -------------
-- R2 二元组影子列：NFKC 后「单字 + 双字」空格分隔 token 串
CREATE TABLE IF NOT EXISTS memories_ngrams (
  memory_id  TEXT PRIMARY KEY,
  ngram_text TEXT NOT NULL
);
-- R2 全文索引：unicode61 索引 ngram_text（rowid = memories.rowid，事实层只追加故稳定）
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts_bigram USING fts5(
  ngram_text,
  tokenize='unicode61'
);
-- R3 trigram 全文索引：独立虚表，可单独开关（1–2 字查询在此路不可用）
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts_trigram USING fts5(
  content,
  tokenize='trigram'
);
"""


def _supersedes_columns(db: Database) -> dict[str, dict]:
    rows = db.read_conn.execute("PRAGMA table_info(supersedes)").fetchall()
    return {row["name"]: dict(row) for row in rows}


def migrate_supersedes_pk(db: Database) -> bool:
    """把旧的主键 (from_id,to_id,recorded_at) 重建为自增 id 主键。返回是否执行了迁移。

    旧表每个 (from_id,to_id,recorded_at) 至多一行（旧主键约束），所以复制不会丢边。
    """
    cols = _supersedes_columns(db)
    if not cols:
        return False  # 新库：由 SCHEMA_SQL 建新结构
    if "id" in cols and cols["id"]["pk"] == 1:
        return False  # 已是新结构

    logger.warning(
        "detected legacy supersedes schema (pk without AUTOINCREMENT id); rebuilding table"
    )
    has_id = "id" in cols
    with db.write_txn() as conn:
        conn.execute("ALTER TABLE supersedes RENAME TO supersedes_legacy")
        conn.execute(SUPERSEDES_TABLE_SQL)
        if has_id:
            conn.execute(
                "INSERT INTO supersedes "
                "(id, from_id, to_id, kind, reason, recorded_at, source_client) "
                "SELECT id, from_id, to_id, kind, reason, recorded_at, source_client "
                "FROM supersedes_legacy ORDER BY id"
            )
        else:
            conn.execute(
                "INSERT INTO supersedes "
                "(from_id, to_id, kind, reason, recorded_at, source_client) "
                "SELECT from_id, to_id, kind, reason, recorded_at, source_client "
                "FROM supersedes_legacy ORDER BY recorded_at"
            )
        conn.execute("DROP TABLE supersedes_legacy")
    logger.warning("supersedes table rebuilt with AUTOINCREMENT id primary key")
    return True


def migrate_derive_state_stages(db: Database) -> bool:
    """把旧 derive_state 的 stage CHECK 扩到含 'fts'（SQLite 不能 ALTER CHECK → 重建）。

    旧表只有 ('embedding','entity','graph')；重建时保留全部水位行。返回是否执行了迁移。
    """
    row = db.read_conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='derive_state'"
    ).fetchone()
    if row is None or not row["sql"]:
        return False  # 新库
    if "'fts'" in row["sql"] or '"fts"' in row["sql"]:
        return False
    logger.warning("detected legacy derive_state stage CHECK; rebuilding to include 'fts'")
    with db.write_txn() as conn:
        conn.execute("ALTER TABLE derive_state RENAME TO derive_state_legacy")
        conn.execute(DERIVE_STATE_TABLE_SQL)
        conn.execute(
            "INSERT INTO derive_state(memory_id, stage, done_at) "
            "SELECT memory_id, stage, done_at FROM derive_state_legacy"
        )
        conn.execute("DROP TABLE derive_state_legacy")
    logger.warning("derive_state table rebuilt with 'fts' stage")
    return True


def init_db(db: Database) -> None:
    """迁移入口：先做 supersedes 主键 / derive_state stage 迁移，再幂等补齐其余结构。"""
    migrate_supersedes_pk(db)
    migrate_derive_state_stages(db)
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
