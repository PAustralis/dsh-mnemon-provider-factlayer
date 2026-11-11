"""连接管理：WAL、synchronous=FULL、busy_timeout=5000、单写锁 + 专用写连接、只读连接按线程复用。"""

from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger("factlayer.db")

PRAGMAS = (
    "PRAGMA journal_mode=WAL;",
    "PRAGMA synchronous=FULL;",
    "PRAGMA busy_timeout=5000;",
    "PRAGMA foreign_keys=ON;",
)


class Database:
    """SQLite 事实层句柄。

    - 所有写事务走 ``write_txn()``：全局 threading.Lock + 专用写连接 + BEGIN IMMEDIATE，
      保证 seq 分配等读-改-写逻辑串行化（部署固定单 worker）。
    - 读走每线程独立的只读复用连接（WAL 下读写不互阻塞）。
    """

    def __init__(self, path: str) -> None:
        self.path = str(path)
        parent = Path(self.path).parent
        if str(parent):
            parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        self._write_conn = self._new_conn()
        self._read_local = threading.local()

    def _new_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path, isolation_level=None, timeout=5.0, check_same_thread=False
        )
        # check_same_thread=False：FastAPI 同步端点跑在线程池工作线程中。
        # 写连接由 self._write_lock 串行保护；读连接按线程独立创建（thread-local）。
        for pragma in PRAGMAS:
            conn.execute(pragma)
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
        self.close_thread_read_conn()
