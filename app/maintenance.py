"""SQLite 运维：WAL 检查点与 FTS5 optimize（后台维护循环 + admin 端点共用）。

- WAL：``Database.wal_checkpoint`` 负责实际 PRAGMA；本模块的后台策略在
  「距上次写入已空闲 N 秒」且距上次尝试超过间隔时执行 ``TRUNCATE``。
  读者持有 WAL 快照时 TRUNCATE 会返回 busy=1：**只记录/记日志，不抛异常**，
  由 ``/health.sqlite.last_checkpoint_error`` 可观测。
- FTS5：``optimize_fts`` 对库内所有 FTS5 虚表执行
  ``INSERT INTO <fts>(<fts>) VALUES('optimize')``；失败逐表捕获，状态写入 meta，
  由 ``/health.fts_optimize`` 可观测。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from typing import Callable

from .db import Database
from .schema import get_meta

logger = logging.getLogger("factlayer.maintenance")

FTS_OPTIMIZE_LAST_AT_KEY = "fts_optimize_last_at"
FTS_OPTIMIZE_LAST_ATTEMPT_KEY = "fts_optimize_last_attempt_at"
FTS_OPTIMIZE_LAST_ERROR_KEY = "fts_optimize_last_error"

_OPTIMIZE_LOCK = threading.Lock()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _meta_int(db: Database, key: str) -> int | None:
    raw = get_meta(db, key)
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def fts_table_names(conn: sqlite3.Connection) -> list[str]:
    """库内全部 FTS5 虚表名（按名字排序；只认 CREATE VIRTUAL TABLE … fts5）。"""
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND sql IS NOT NULL "
        "AND lower(sql) LIKE 'create virtual table%' "
        "AND lower(sql) LIKE '%fts5%' "
        "ORDER BY name"
    ).fetchall()
    return [str(row[0]) for row in rows]


def _run_optimize(conn: sqlite3.Connection, table: str) -> None:
    """FTS5 optimize 单表语句（独立函数便于测试 monkeypatch 计数/注入失败）。"""
    quoted = '"' + table.replace('"', '""') + '"'
    conn.execute(f"INSERT INTO {quoted}({quoted}) VALUES('optimize')")


def _record_optimize_state(
    db: Database, *, at_ms: int, error: str | None
) -> None:
    """把 optimize 状态写入 meta（独立事务；失败只记日志，不影响主流程）。"""
    values = {
        FTS_OPTIMIZE_LAST_AT_KEY: str(at_ms),
        FTS_OPTIMIZE_LAST_ATTEMPT_KEY: str(at_ms),
        FTS_OPTIMIZE_LAST_ERROR_KEY: error or "",
    }
    with db.write_txn() as conn:
        for key, value in values.items():
            conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )


def fts_optimize_state(db: Database) -> dict:
    """``/health.fts_optimize``：最近成功时间、最近错误（失败不静默）。"""
    return {
        "last_at": _meta_int(db, FTS_OPTIMIZE_LAST_AT_KEY),
        "last_attempt_at": _meta_int(db, FTS_OPTIMIZE_LAST_ATTEMPT_KEY),
        "last_error": get_meta(db, FTS_OPTIMIZE_LAST_ERROR_KEY) or None,
    }


def optimize_fts(db: Database, *, reason: str = "manual") -> dict:
    """对所有启用的 FTS5 虚表执行 optimize；失败逐表记录，绝不抛出。

    返回 ``{ok, optimized, errors, at, reason}``。状态同时写入 meta：
    ``fts_optimize_last_at``（本次尝试时间）、``fts_optimize_last_error``。
    """
    started_at = _now_ms()
    optimized: list[str] = []
    errors: list[str] = []
    with _OPTIMIZE_LOCK:
        try:
            names = fts_table_names(db.read_conn)
        except Exception as exc:  # 列表都读不到：记录错误，服务继续
            names = []
            errors.append(f"list fts tables: {type(exc).__name__}: {exc}")
            logger.exception("FTS5 optimize：读取虚表列表失败（reason=%s）", reason)
        for name in names:
            try:
                with db.write_txn() as conn:
                    _run_optimize(conn, name)
                optimized.append(name)
            except Exception as exc:
                message = f"{name}: {type(exc).__name__}: {exc}"
                errors.append(message)
                logger.exception(
                    "FTS5 optimize 失败 table=%s（reason=%s，已记录到 /health）",
                    name,
                    reason,
                )
        error_text = "; ".join(errors) if errors else None
        try:
            _record_optimize_state(db, at_ms=started_at, error=error_text)
        except Exception:
            # 连状态都写不进去（如磁盘满）：至少留下服务端日志，不拖垮调用方。
            logger.exception("FTS5 optimize 状态写入 meta 失败（reason=%s）", reason)
        if errors:
            logger.error(
                "FTS5 optimize 部分失败（reason=%s）：%s", reason, error_text
            )
        else:
            logger.info(
                "FTS5 optimize 完成（reason=%s）：tables=%s", reason, optimized
            )
    return {
        "ok": not errors,
        "optimized": optimized,
        "errors": errors,
        "at": started_at,
        "reason": reason,
    }


class Maintenance:
    """后台维护策略：WAL TRUNCATE（空闲时）与 FTS5 optimize（按日）。

    由 ``app.main`` 的现有后台线程每 ``DERIVE_LOOP_INTERVAL_SECONDS`` 调用一次
    ``run_due()``；所有失败只记日志/状态，不向上抛。
    """

    def __init__(
        self,
        db: Database,
        *,
        checkpoint_interval_seconds: int = 300,
        checkpoint_idle_seconds: float = 60.0,
        fts_optimize_interval_seconds: int = 86400,
        clock: Callable[[], float] | None = None,
        wall_clock: Callable[[], float] | None = None,
    ) -> None:
        self.db = db
        self.checkpoint_interval_seconds = int(checkpoint_interval_seconds)
        self.checkpoint_idle_seconds = max(0.0, float(checkpoint_idle_seconds))
        self.fts_optimize_interval_seconds = int(fts_optimize_interval_seconds)
        self._clock = clock or time.monotonic
        self._wall_clock = wall_clock or time.time
        # 启动后先等一个完整间隔再尝试；有 meta 记录则按上次尝试续算。
        self._last_checkpoint_attempt = self._clock()
        self._last_optimize_attempt = self._initial_optimize_attempt()

    def _initial_optimize_attempt(self) -> float:
        """用 wall-clock 秒表示的上次 optimize 尝试时间（无记录 = 当前/已有值）。"""
        raw = _meta_int(self.db, FTS_OPTIMIZE_LAST_ATTEMPT_KEY)
        if raw is None:
            return getattr(self, "_last_optimize_attempt", self._wall_clock())
        return raw / 1000.0

    def maybe_checkpoint_truncate(self) -> dict | None:
        """无写入空闲且到期时执行 TRUNCATE；不满足条件或失败返回 None。"""
        if self.checkpoint_interval_seconds <= 0:
            return None
        now = self._clock()
        if now - self._last_checkpoint_attempt < self.checkpoint_interval_seconds:
            return None
        if self.db.seconds_since_last_write() < self.checkpoint_idle_seconds:
            return None
        self._last_checkpoint_attempt = now
        try:
            result = self.db.wal_checkpoint("TRUNCATE")
        except Exception:
            # Database.wal_checkpoint 已记录 last_checkpoint_error；这里只保证不抛。
            logger.exception("后台 WAL TRUNCATE 检查点失败（不抛出，/health 可观测）")
            return None
        if result["busy"]:
            logger.warning(
                "后台 WAL TRUNCATE 未完成（busy=1，读者/写者占用；"
                "last_checkpoint_error=%s）",
                self.db.last_checkpoint_error,
            )
        else:
            logger.info(
                "后台 WAL TRUNCATE 完成：log_pages=%s checkpointed_pages=%s",
                result["log_pages"],
                result["checkpointed_pages"],
            )
        return result

    def maybe_optimize_fts(self) -> dict | None:
        """按 ``DSH_FTS_OPTIMIZE_INTERVAL_SECONDS`` 到期时执行一次 optimize。"""
        if self.fts_optimize_interval_seconds <= 0:
            return None
        # admin 重建也会 optimize：以 meta 里的最近尝试为准，避免刚做过又做一遍。
        last_attempt = max(
            self._last_optimize_attempt,
            self._initial_optimize_attempt(),
        )
        now = self._wall_clock()
        if now - last_attempt < self.fts_optimize_interval_seconds:
            return None
        self._last_optimize_attempt = now
        try:
            return optimize_fts(self.db, reason="maintenance")
        except Exception:  # optimize_fts 内部已兜底；这里再防一层
            logger.exception("后台 FTS5 optimize 失败（不抛出）")
            return None

    def run_due(self) -> dict:
        """后台线程每轮调用：分别检查两项维护（互不影响）。"""
        return {
            "wal_checkpoint": self.maybe_checkpoint_truncate(),
            "fts_optimize": self.maybe_optimize_fts(),
        }
