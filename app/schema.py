"""全部建表 SQL + FTS5 虚表与触发器 + 迁移入口。

依据 docs/01-事实层设计.md（定稿 v7 + 评审修订）与 docs/02-检索层设计.md（v8）：

- memories 只追加：用 RAISE(ABORT) 触发器在数据库层禁止 UPDATE/DELETE。
- **全局单调序 ``gseq``**（v8 §6）：``gseq INTEGER PRIMARY KEY AUTOINCREMENT``，
  是 rowid 别名；``id TEXT UNIQUE NOT NULL`` 保留为对外稳定标识；``seq`` 语义不变
  （会话内 0 起连续序号）。tie-break 一律 ``gseq DESC``。
- supersedes 语义为「撤销作废也走追加」：同一 (from_id,to_id) 可有多条边，
  最新一条由自增 ``id`` 决定（**只按 id DESC**，不依赖 recorded_at——时钟回拨会选错边）。
- 派生侧（derive_state / embeddings / entities / FTS5）全部可弃、可清空重建。

迁移入口：``init_db()`` 先检测旧结构并重建（memories 的 gseq 主键、supersedes 的
自增 id 主键、derive_state 的 stage CHECK、embeddings 的 model_id/dim 列），
再幂等补齐其余结构。项目尚无生产数据时也可以「删库 + 派生重建」，见 README。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .db import Database

if TYPE_CHECKING:  # pragma: no cover - 仅类型提示，避免 schema ↔ derive 循环导入
    from .derive import DerivePipeline

logger = logging.getLogger("factlayer.schema")

# 迁移后派生重建状态（/health 与 init_db 共用，避免「静默搜不到」）
MIGRATION_NEEDS_REBUILD_KEY = "migration_needs_rebuild"
MIGRATION_REBUILD_KEY = "migration_rebuild"

SUPERSEDES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS supersedes (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,  -- 追加顺序；最新边只按 id DESC
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

# gseq 是 rowid 别名；id 只是对外稳定标识（UNIQUE NOT NULL）。
MEMORIES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS memories (
  gseq         INTEGER PRIMARY KEY AUTOINCREMENT,  -- 全局单调序（VACUUM 不变）
  id           TEXT UNIQUE NOT NULL,                -- 对外稳定标识
  client_write_id TEXT NOT NULL UNIQUE,
  content      TEXT NOT NULL,
  recorded_at  INTEGER NOT NULL,
  session_id   TEXT NOT NULL,
  seq          INTEGER NOT NULL,                    -- 会话内 0 起连续序号（语义不变）
  event_time   TEXT,
  event_time_precision TEXT CHECK (event_time_precision IN ('year','month','day','instant')),
  source_client TEXT NOT NULL,
  CHECK ((event_time IS NULL) = (event_time_precision IS NULL))
);
"""

MEMORIES_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_memories_session ON memories(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_memories_time ON memories(recorded_at DESC);
"""

# 数据库层强制只追加：任何 UPDATE/DELETE 直接 ABORT
MEMORIES_TRIGGER_SQL = """
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
"""

# FTS5 全文索引（外部内容表，触发器同步；派生侧，可弃可重建）。
# memories 的 rowid 就是 gseq（INTEGER PRIMARY KEY 别名），迁移回填时保持稳定。
FTS_TABLE_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
  content,
  content='memories',
  content_rowid='rowid'
);
"""

FTS_TRIGGER_SQL = """
CREATE TRIGGER IF NOT EXISTS memories_fts_ai AFTER INSERT ON memories BEGIN
  INSERT INTO memories_fts(rowid, content) VALUES (new.rowid, new.content);
END;
CREATE TRIGGER IF NOT EXISTS memories_fts_ad AFTER DELETE ON memories BEGIN
  INSERT INTO memories_fts(memories_fts, rowid, content) VALUES('delete', old.rowid, old.content);
END;
CREATE TRIGGER IF NOT EXISTS memories_fts_au AFTER UPDATE ON memories BEGIN
  INSERT INTO memories_fts(memories_fts, rowid, content) VALUES('delete', old.rowid, old.content);
  INSERT INTO memories_fts(rowid, content) VALUES (new.rowid, new.content);
END;
"""

# 单混合列（v8 §3）：与 research/recall/recall_eval.py::bigrams_of 同构。
# 不拆 uni/bi 两列——拆列会改变 BM25 的 IDF 与文档长度归一，排序不再等价。
NGRAM_TABLE_SQL = """
-- R2 二元组影子列：NFKC 后与基线 bigrams_of 完全相同的 token 串（同一列）
CREATE TABLE IF NOT EXISTS memories_ngrams (
  memory_id  TEXT PRIMARY KEY,
  ngram_text TEXT NOT NULL
);
-- R2 全文索引：unicode61 索引单列 ngram_text（rowid = memories.rowid = gseq）
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts_bigram USING fts5(
  ngram_text,
  tokenize='unicode61'
);
-- trigram 全文索引：独立虚表，可单独开关（v8 默认不启用；1–2 字查询在此路不可用）
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts_trigram USING fts5(
  content,
  tokenize='trigram'
);
"""

SCHEMA_SQL = f"""
{MEMORIES_TABLE_SQL}
{MEMORIES_INDEX_SQL}
{MEMORIES_TRIGGER_SQL}

-- 作废边：追加式；最新边只按自增 id 决定 (from_id,to_id) 状态
{SUPERSEDES_TABLE_SQL}
-- 每个 pair 取最新边：只按 id DESC（recorded_at 不参与，时钟回拨会选错边）
CREATE INDEX IF NOT EXISTS idx_supersedes_pair
  ON supersedes(from_id, to_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_supersedes_to ON supersedes(to_id, id DESC);

-- 派生侧水位表（stage 含检索层的 fts：生成 ngrams + 写 FTS5，见 docs/02 §3.1）
{DERIVE_STATE_TABLE_SQL}
CREATE TABLE IF NOT EXISTS embeddings (
  memory_id TEXT PRIMARY KEY,
  vector    BLOB NOT NULL,
  model_id  TEXT NOT NULL DEFAULT '',
  dim       INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS entities (
  memory_id TEXT NOT NULL,
  entity    TEXT NOT NULL,
  PRIMARY KEY (memory_id, entity)
);
CREATE INDEX IF NOT EXISTS idx_entities ON entities(entity);

-- FTS5 全文索引（外部内容表，触发器同步；派生侧，可弃可重建）
{FTS_TABLE_SQL}
{FTS_TRIGGER_SQL}

-- 元信息表（last_backup_at / embedding_model / embedding_dim / retrieval:eval）
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- ---- 检索层派生结构（全部可弃、可重建；不在 memories 上加列） -------------
{NGRAM_TABLE_SQL}
"""

# 旧表（v3.1 及以前）的 memories 结构：id TEXT PRIMARY KEY，无 gseq
# （迁移函数按 rowid 回填，不依赖列顺序）


def _table_columns(db: Database, table: str) -> dict[str, dict]:
    rows = db.read_conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row["name"]: dict(row) for row in rows}


def _supersedes_columns(db: Database) -> dict[str, dict]:
    return _table_columns(db, "supersedes")


def migrate_memories_gseq(db: Database) -> bool:
    """把旧 ``memories(id TEXT PRIMARY KEY)`` 重建为 ``gseq INTEGER PRIMARY KEY``。

    SQLite 改主键必须重建表。步骤（顺序即设计 §6）：
    1. DROP 防写触发器与 FTS 触发器（否则重建期间 RAISE/同步触发器会挡路），
       同时 DROP 旧索引与旧 FTS 表（索引名会跟着 RENAME 走，先清掉）；
    2. ``ALTER TABLE memories RENAME TO memories_legacy``，建新表；
    3. ``INSERT … SELECT`` 时按 rowid 显式填 gseq（旧 rowid 即全局追加顺序）；
    4. 重建 FTS（external content，rowid 稳定后整体 rebuild）；
    5. 重建索引与触发器；
    6. 更新 sqlite_sequence，保证后续 AUTOINCREMENT 从 max(gseq)+1 开始。

    返回是否执行了迁移。
    """
    cols = _table_columns(db, "memories")
    if not cols:
        return False  # 新库：由 SCHEMA_SQL 建新结构
    if "gseq" in cols:
        return False  # 已是新结构

    logger.warning(
        "detected legacy memories schema (id TEXT PRIMARY KEY); rebuilding with gseq"
    )
    with db.write_txn() as conn:
        # 1. 清掉所有会干扰重建的触发器 / 索引 / FTS 表
        for trigger in (
            "memories_no_update",
            "memories_no_delete",
            "memories_fts_ai",
            "memories_fts_ad",
            "memories_fts_au",
        ):
            conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        conn.execute("DROP INDEX IF EXISTS idx_memories_session")
        conn.execute("DROP INDEX IF EXISTS idx_memories_time")
        conn.execute("DROP TABLE IF EXISTS memories_fts")

        # 2. 旧表改名，建新表（不能 IF NOT EXISTS 之外再加 id 主键）
        conn.execute("ALTER TABLE memories RENAME TO memories_legacy")
        conn.execute(
            """
            CREATE TABLE memories (
              gseq         INTEGER PRIMARY KEY AUTOINCREMENT,
              id           TEXT UNIQUE NOT NULL,
              client_write_id TEXT NOT NULL UNIQUE,
              content      TEXT NOT NULL,
              recorded_at  INTEGER NOT NULL,
              session_id   TEXT NOT NULL,
              seq          INTEGER NOT NULL,
              event_time   TEXT,
              event_time_precision TEXT
                CHECK (event_time_precision IN ('year','month','day','instant')),
              source_client TEXT NOT NULL,
              CHECK ((event_time IS NULL) = (event_time_precision IS NULL))
            )
            """
        )
        # 3. 按旧 rowid 显式回填 gseq（顺序与旧追加顺序一致）
        conn.execute(
            "INSERT INTO memories "
            "(gseq, id, client_write_id, content, recorded_at, session_id, seq, "
            " event_time, event_time_precision, source_client) "
            "SELECT rowid, id, client_write_id, content, recorded_at, session_id, seq, "
            " event_time, event_time_precision, source_client "
            "FROM memories_legacy ORDER BY rowid"
        )
        conn.execute("DROP TABLE memories_legacy")

        # 4. 重建索引
        conn.execute(
            "CREATE INDEX idx_memories_session ON memories(session_id, seq)"
        )
        conn.execute(
            "CREATE INDEX idx_memories_time ON memories(recorded_at DESC)"
        )

        # 5. 重建 FTS（external content rowid 稳定）+ 从事实表整体 rebuild
        conn.execute(
            "CREATE VIRTUAL TABLE memories_fts USING fts5("
            "content, content='memories', content_rowid='rowid')"
        )
        conn.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")

        # 6. 重建触发器
        for statement in _split_sql(FTS_TRIGGER_SQL):
            conn.execute(statement)
        for statement in _split_sql(MEMORIES_TRIGGER_SQL):
            conn.execute(statement)

        # 7. 更新 sqlite_sequence（显式 gseq 回填后保证后续自增不撞号）
        max_gseq = conn.execute("SELECT COALESCE(MAX(gseq), 0) FROM memories").fetchone()[0]
        updated = conn.execute(
            "UPDATE sqlite_sequence SET seq=? WHERE name='memories'", (max_gseq,)
        ).rowcount
        if not updated:
            conn.execute(
                "INSERT INTO sqlite_sequence(name, seq) VALUES('memories', ?)",
                (max_gseq,),
            )

    logger.warning("memories table rebuilt with gseq INTEGER PRIMARY KEY AUTOINCREMENT")
    return True


def _split_sql(script: str) -> list[str]:
    """按 sqlite3.complete_statement 切分多语句脚本（BEGIN…END; 不被内部 ; 切开）。"""
    statements: list[str] = []
    buffer = ""
    for line in script.splitlines(keepends=True):
        if not buffer and not line.strip():
            continue
        buffer += line
        if sqlite3.complete_statement(buffer):
            statements.append(buffer.strip())
            buffer = ""
    if buffer.strip():
        statements.append(buffer.strip())
    return statements


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


def migrate_supersedes_pair_index(db: Database) -> bool:
    """把 idx_supersedes_pair 的定义收敛为只按 ``id DESC``（不再含 recorded_at）。"""
    row = db.read_conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_supersedes_pair'"
    ).fetchone()
    if row is None:
        return False  # 由 SCHEMA_SQL 建新索引
    sql = (row["sql"] or "").lower()
    if "recorded_at" not in sql and "id desc" in sql:
        return False  # 已是新定义
    logger.warning("detected legacy idx_supersedes_pair; rebuilding index by id DESC only")
    with db.write_txn() as conn:
        conn.execute("DROP INDEX IF EXISTS idx_supersedes_pair")
        conn.execute(
            "CREATE INDEX idx_supersedes_pair ON supersedes(from_id, to_id, id DESC)"
        )
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


def migrate_embeddings_columns(db: Database) -> bool:
    """embeddings 增加 ``model_id`` / ``dim``（v8 §7：按行存模型与维度）。

    旧行回填 dim = len(vector)/4；model_id 取 meta.embedding_model（若有）。
    """
    cols = _table_columns(db, "embeddings")
    if not cols:
        return False  # 新库
    if "model_id" in cols and "dim" in cols:
        return False
    model_row = db.read_conn.execute(
        "SELECT value FROM meta WHERE key='embedding_model'"
    ).fetchone()
    stored_model = model_row["value"] if model_row is not None else ""
    with db.write_txn() as conn:
        if "model_id" not in cols:
            conn.execute(
                "ALTER TABLE embeddings ADD COLUMN model_id TEXT NOT NULL DEFAULT ''"
            )
        if "dim" not in cols:
            conn.execute(
                "ALTER TABLE embeddings ADD COLUMN dim INTEGER NOT NULL DEFAULT 0"
            )
        conn.execute(
            "UPDATE embeddings SET dim = LENGTH(vector)/4 WHERE dim = 0 AND LENGTH(vector)%4 = 0"
        )
        if stored_model:
            conn.execute(
                "UPDATE embeddings SET model_id = ? WHERE model_id = ''",
                (stored_model,),
            )
    logger.warning("embeddings table extended with model_id/dim columns")
    return True


@dataclass
class InitReport:
    """``init_db()`` 的结果：是否迁移、是否做了派生重建、重建是否成功。"""

    migrated_memories: bool = False
    memories: int = 0
    rebuild_needed: bool = False
    rebuild_attempted: bool = False
    rebuild_ok: bool | None = None
    rebuild_counts: dict[str, int] | None = None
    embedding_skipped: str | None = None
    embedding_error: str | None = None
    rebuild_error: str | None = None


def _index_row_counts(db: Database) -> tuple[int, int, int]:
    """(事实行数, ngram 影子行数, bigram FTS 行数)——三者应恒等。"""
    conn = db.read_conn
    memories = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    ngrams = conn.execute("SELECT COUNT(*) FROM memories_ngrams").fetchone()[0]
    bigram = conn.execute("SELECT COUNT(*) FROM memories_fts_bigram").fetchone()[0]
    return int(memories), int(ngrams), int(bigram)


def _rebuild_after_migration(
    db: Database,
    pipeline: DerivePipeline | None,
    *,
    migrated_memories: bool,
    memories: int,
    ngrams: int,
    bigram: int,
) -> InitReport:
    """迁移/索引缺失后的强制派生重建，状态写 meta 供 ``/health`` 暴露。

    - 触发条件：刚完成 memories 迁移且库里有数据；或索引行数 != 事实行数
      （覆盖「先手工 migrate_memories_gseq、再 init_db」以及历史上已发生的静默缺索引）。
    - 重建失败**不抛异常**（服务仍启动，但必须可观测）：ERROR 日志 + meta
      ``migration_needs_rebuild=1`` + ``migration_rebuild.status=failed``。
    """
    index_gap = memories > 0 and (ngrams != memories or bigram != memories)
    needed = (migrated_memories and memories > 0) or index_gap
    if not needed:
        return InitReport(migrated_memories=migrated_memories, memories=memories)

    logger.warning(
        "检测到派生索引不完整，启动时强制全量重建：memories=%d ngrams=%d "
        "bigram_fts=%d（migrated=%s）",
        memories,
        ngrams,
        bigram,
        migrated_memories,
    )
    # 先落标记：重建中途崩溃（进程被杀）时 /health 仍能显示需要补跑
    set_meta(db, MIGRATION_NEEDS_REBUILD_KEY, "1")
    try:
        if pipeline is None:
            # init_db 的直接调用方（脚本/测试/研究代码）没有注入 provider：
            # 用无 provider 的 pipeline 重建 ngram/FTS（embedding 跳过并记录原因）。
            from .derive import DerivePipeline

            pipeline = DerivePipeline(db, stub_success=True)
        outcome = pipeline.rebuild_after_migration()
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        logger.error(
            "迁移后派生重建失败（/health migration_needs_rebuild=true）：%s",
            error,
            exc_info=True,
        )
        set_meta(
            db,
            MIGRATION_REBUILD_KEY,
            json.dumps(
                {"status": "failed", "at": int(time.time() * 1000), "error": error},
                ensure_ascii=False,
            ),
        )
        return InitReport(
            migrated_memories=migrated_memories,
            memories=memories,
            rebuild_needed=True,
            rebuild_attempted=True,
            rebuild_ok=False,
            rebuild_error=error,
        )

    detail = {
        "status": "ok",
        "at": int(time.time() * 1000),
        "counts": outcome.get("counts"),
        "embedding_skipped": outcome.get("embedding_skipped"),
        "embedding_error": outcome.get("embedding_error"),
    }
    set_meta(db, MIGRATION_NEEDS_REBUILD_KEY, "0")
    set_meta(db, MIGRATION_REBUILD_KEY, json.dumps(detail, ensure_ascii=False))
    logger.info(
        "迁移后派生重建完成：%s（embedding 跳过原因：%s；embedding 错误：%s）",
        detail["counts"],
        detail["embedding_skipped"],
        detail["embedding_error"],
    )
    return InitReport(
        migrated_memories=migrated_memories,
        memories=memories,
        rebuild_needed=True,
        rebuild_attempted=True,
        rebuild_ok=True,
        rebuild_counts=outcome.get("counts"),
        embedding_skipped=outcome.get("embedding_skipped"),
        embedding_error=outcome.get("embedding_error"),
    )


def init_db(
    db: Database, *, pipeline: DerivePipeline | None = None
) -> InitReport:
    """迁移入口：先重建旧结构，再幂等补齐其余结构。

    结构就绪后，若检测到「刚迁移」或「派生索引行数与事实行数不一致」，会**在启动时
    强制跑一次完整派生重建**（ngram + FTS；有 embedding provider 时连同向量），
    避免 legacy 库迁移后 bigram 索引为空、旧记忆永久搜不到。失败状态由
    ``/health`` 的 ``migration_needs_rebuild`` / ``alerts`` 暴露。
    """
    migrated_memories = migrate_memories_gseq(db)
    migrate_supersedes_pk(db)
    migrate_supersedes_pair_index(db)
    migrate_derive_state_stages(db)
    migrate_embeddings_columns(db)
    db.execute_script(SCHEMA_SQL)
    memories, ngrams, bigram = _index_row_counts(db)
    return _rebuild_after_migration(
        db,
        pipeline,
        migrated_memories=migrated_memories,
        memories=memories,
        ngrams=ngrams,
        bigram=bigram,
    )


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
