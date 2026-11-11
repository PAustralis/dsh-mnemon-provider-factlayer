"""连接管理：WAL、synchronous=FULL、busy_timeout=5000、单写锁 + 专用写连接、只读连接按线程复用。"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

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

    @contextmanager
    def write_txn(self):
        """串行写事务：BEGIN IMMEDIATE ... COMMIT/ROLLBACK。"""
        with self._write_lock:
            conn = self._write_conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    def execute_script(self, script: str) -> None:
        """executescript 会自带隐式提交，不能嵌在 write_txn 里；仅建库/迁移用。"""
        with self._write_lock:
            self._write_conn.executescript(script)

    def vacuum_into(self, dest: str) -> None:
        """VACUUM INTO（备份用）。必须在无活动事务下执行，持有写锁以串行化。"""
        with self._write_lock:
            self._write_conn.execute("VACUUM INTO ?", (dest,))

    def close(self) -> None:
        with self._write_lock:
            self._write_conn.close()
        conn = getattr(self._read_local, "conn", None)
        if conn is not None:
            conn.close()
            self._read_local.conn = None
