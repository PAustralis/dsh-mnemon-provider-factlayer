"""连接管理：WAL、synchronous=FULL、busy_timeout=5000、单写锁 + 专用写连接、只读连接按线程复用。

运维补充（2026-11-12）：

- 每个连接显式设置 ``PRAGMA wal_autocheckpoint``（默认 1000 页，环境变量
  ``DSH_WAL_AUTOCHECKPOINT_PAGES`` 覆盖）。
- 维护动作 ``wal_checkpoint(mode)``：默认 PASSIVE（不阻塞读者）；后台维护用
  TRUNCATE（读者/写者占用时返回 busy=1 并记录 ``last_checkpoint_error``，不抛）。
  检查点走**独立连接**且 busy_timeout=0，避免维护动作把写路径卡在等待锁上。
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger("factlayer.db")

DEFAULT_WAL_AUTOCHECKPOINT_PAGES = 1000
CHECKPOINT_MODES = ("PASSIVE", "FULL", "RESTART", "TRUNCATE")

PRAGMAS = (
    "PRAGMA journal_mode=WAL;",
    "PRAGMA synchronous=FULL;",
    "PRAGMA busy_timeout=5000;",
    "PRAGMA foreign_keys=ON;",
)


def _autocheckpoint_pages_from_env() -> int:
    """``DSH_WAL_AUTOCHECKPOINT_PAGES`` → 页数；非法值记日志并回退默认值。"""
    raw = os.environ.get("DSH_WAL_AUTOCHECKPOINT_PAGES")
    if raw is None or not raw.strip():
        return DEFAULT_WAL_AUTOCHECKPOINT_PAGES
    try:
        pages = int(raw.strip())
    except ValueError:
        logger.warning(
            "DSH_WAL_AUTOCHECKPOINT_PAGES=%r 不是整数，回退默认 %d 页",
            raw,
            DEFAULT_WAL_AUTOCHECKPOINT_PAGES,
        )
        return DEFAULT_WAL_AUTOCHECKPOINT_PAGES
    if pages < 0:
        logger.warning(
            "DSH_WAL_AUTOCHECKPOINT_PAGES=%d 不能为负，回退默认 %d 页",
            pages,
            DEFAULT_WAL_AUTOCHECKPOINT_PAGES,
        )
        return DEFAULT_WAL_AUTOCHECKPOINT_PAGES
    return pages


def _now_ms() -> int:
    return int(time.time() * 1000)


class Database:
    """SQLite 事实层句柄。

    - 所有写事务走 ``write_txn()``：全局 threading.Lock + 专用写连接 + BEGIN IMMEDIATE，
      保证 seq 分配等读-改-写逻辑串行化（部署固定单 worker）。
    - 读走每线程独立的只读复用连接（WAL 下读写不互阻塞）。
    - 检查点走独立连接（不持写锁、busy_timeout=0）：PASSIVE 不阻塞读者，
      TRUNCATE 在读者持有快照时快速返回 busy=1，而不是卡住写路径 5 秒。
    """

    def __init__(
        self,
        path: str,
        *,
        wal_autocheckpoint_pages: int | None = None,
        checkpoint_busy_timeout_ms: int = 0,
    ) -> None:
        self.path = str(path)
        parent = Path(self.path).parent
        if str(parent):
            parent.mkdir(parents=True, exist_ok=True)
        self.wal_autocheckpoint_pages = (
            _autocheckpoint_pages_from_env()
            if wal_autocheckpoint_pages is None
            else max(0, int(wal_autocheckpoint_pages))
        )
        self._write_lock = threading.Lock()
        self._checkpoint_lock = threading.Lock()
        self._checkpoint_state_lock = threading.Lock()
        self.last_checkpoint_at: int | None = None
        self.last_checkpoint_mode: str | None = None
        self.last_checkpoint_error: str | None = None
        self.last_checkpoint_result: dict | None = None
        self._last_write_monotonic = 0.0
        self._write_conn = self._new_conn()
        self._checkpoint_conn = self._new_conn(
            busy_timeout_ms=checkpoint_busy_timeout_ms
        )
        self._read_local = threading.local()

    def _new_conn(self, busy_timeout_ms: int | None = None) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path, isolation_level=None, timeout=5.0, check_same_thread=False
        )
        # check_same_thread=False：FastAPI 同步端点跑在线程池工作线程中。
        # 写连接由 self._write_lock 串行保护；读连接按线程独立创建（thread-local）。
        for pragma in PRAGMAS:
            conn.execute(pragma)
        # 每个连接独立生效的自动检查点阈值（0 = 关闭自动检查点）。
        conn.execute(f"PRAGMA wal_autocheckpoint={int(self.wal_autocheckpoint_pages)}")
        if busy_timeout_ms is not None:
            # 检查点连接专用：不要为等读者 Snapshot 而阻塞维护/写路径。
            conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        conn.row_factory = sqlite3.Row
        return conn

    @property
    def read_conn(self) -> sqlite3.Connection:
        conn = getattr(self._read_local, "conn", None)
        if conn is None:
            conn = self._new_conn()
            self._read_local.conn = conn
        return conn

    def _commit(self, conn: sqlite3.Connection) -> None:
        """独立方法便于测试注入 COMMIT 失败（sqlite3.Connection 实例不可 monkeypatch）。"""
        conn.execute("COMMIT")

    def _rollback(self, conn: sqlite3.Connection) -> None:
        """尽最大努力回滚。连接若已被 SQLite 自动回滚（in_transaction=False）则跳过。"""
        if not conn.in_transaction:
            return
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            # 回滚都失败时保留原始异常（调用方 raise），只记录服务端日志
            logger.exception("ROLLBACK failed after transaction error")

    @contextmanager
    def write_txn(self):
        """串行写事务：BEGIN IMMEDIATE ... COMMIT/ROLLBACK。

        COMMIT 失败（磁盘满 / IO 错误）时也必须 ROLLBACK：否则连接停在打开事务态，
        之后所有 BEGIN IMMEDIATE 都会报 "cannot start a transaction within a transaction"，
        写入永久失败到进程重启。
        """
        with self._write_lock:
            conn = self._write_conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                self._rollback(conn)
                raise
            else:
                try:
                    self._commit(conn)
                except BaseException:
                    self._rollback(conn)
                    raise
                # 仅供后台维护判断「无写入空闲」；时间基准用单调时钟。
                self._last_write_monotonic = time.monotonic()

    def executemany(self, conn: sqlite3.Connection, sql: str, params) -> None:
        """批量执行入口（独立方法便于测试统计调用次数）。"""
        conn.executemany(sql, params)

    def seconds_since_last_write(self, now: float | None = None) -> float:
        """距离最近一次成功 COMMIT 的秒数；从未写入过返回很大值（视为空闲）。"""
        current = time.monotonic() if now is None else float(now)
        if self._last_write_monotonic <= 0:
            return float("inf")
        return max(0.0, current - self._last_write_monotonic)

    def wal_checkpoint(self, mode: str = "PASSIVE") -> dict:
        """执行 ``PRAGMA wal_checkpoint(<mode>)``，并记录可观测状态。

        返回 ``{busy, log_pages, checkpointed_pages}``。
        - PASSIVE（默认）不阻塞读者；TRUNCATE 在读者/写者占用时返回 busy=1。
        - busy=1 不算异常：记录 ``last_checkpoint_error`` 并照常返回，供后台维护和
          ``/health`` 观测；真正的 SQLite 错误记录后向上抛（由 API 错误处理器兜底）。
        """
        normalized = (mode or "PASSIVE").upper()
        if normalized not in CHECKPOINT_MODES:
            raise ValueError(
                f"未知 WAL checkpoint mode={mode!r}；可用：{', '.join(CHECKPOINT_MODES)}"
            )
        with self._checkpoint_lock:
            try:
                row = self._checkpoint_conn.execute(
                    f"PRAGMA wal_checkpoint({normalized})"
                ).fetchone()
            except sqlite3.Error as exc:
                self._record_checkpoint(
                    normalized,
                    None,
                    f"{type(exc).__name__}: {exc}",
                )
                logger.exception("wal_checkpoint(%s) failed", normalized)
                raise
            busy = int(row[0]) if row is not None else 0
            log_pages = int(row[1]) if row is not None else -1
            checkpointed_pages = int(row[2]) if row is not None else -1
            result = {
                "busy": busy,
                "log_pages": log_pages,
                "checkpointed_pages": checkpointed_pages,
            }
            error = None
            if busy:
                error = (
                    f"wal_checkpoint({normalized}) busy=1"
                    f"（log_pages={log_pages} checkpointed_pages={checkpointed_pages}）："
                    "可能有读者持有 WAL 快照或写者占用，TRUNCATE 未能完成"
                )
                logger.warning("%s", error)
            self._record_checkpoint(normalized, result, error)
            return result

    def _record_checkpoint(
        self, mode: str, result: dict | None, error: str | None
    ) -> None:
        with self._checkpoint_state_lock:
            self.last_checkpoint_at = _now_ms()
            self.last_checkpoint_mode = mode
            self.last_checkpoint_result = result
            self.last_checkpoint_error = error

    def checkpoint_state(self) -> dict:
        """检查点可观测状态（/health 的 sqlite 段使用）。"""
        with self._checkpoint_state_lock:
            return {
                "last_checkpoint_at": self.last_checkpoint_at,
                "last_checkpoint_mode": self.last_checkpoint_mode,
                "last_checkpoint_error": self.last_checkpoint_error,
                "last_checkpoint_result": (
                    dict(self.last_checkpoint_result)
                    if self.last_checkpoint_result is not None
                    else None
                ),
            }

    def file_sizes(self) -> dict:
        """主库与 WAL 文件字节数（WAL 下直接看 .db 大小会漏掉未检查点数据）。"""
        if self.path == ":memory:":
            return {"db_bytes": 0, "wal_bytes": 0}
        db_file = Path(self.path)
        wal_file = Path(self.path + "-wal")
        return {
            "db_bytes": db_file.stat().st_size if db_file.exists() else 0,
            "wal_bytes": wal_file.stat().st_size if wal_file.exists() else 0,
        }

    def execute_script(self, script: str) -> None:
        """executescript 会自带隐式提交，不能嵌在 write_txn 里；仅建库/迁移用。"""
        with self._write_lock:
            self._write_conn.executescript(script)

    def vacuum_into(self, dest: str) -> None:
        """VACUUM INTO（备份用）。必须在无活动事务下执行，持有写锁以串行化。"""
        with self._write_lock:
            self._write_conn.execute("VACUUM INTO ?", (dest,))

    def close_thread_read_conn(self) -> None:
        """关闭**当前线程**的复用读连接（后台线程退出前调用，避免句柄泄漏）。

        ``close()`` 只能关掉调用者线程的连接；任何自建线程（健康快照刷新等）
        必须在自己的线程里调用本方法。
        """
        conn = getattr(self._read_local, "conn", None)
        if conn is not None:
            conn.close()
            self._read_local.conn = None

    def close(self) -> None:
        with self._write_lock:
            self._write_conn.close()
        with self._checkpoint_lock:
            self._checkpoint_conn.close()
        self.close_thread_read_conn()
