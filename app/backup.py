"""备份：VACUUM INTO backups/backup-YYYYMMDD.db（WAL 下直接拷 .db 会漏 -wal 数据）。

- 写入原子性：先 VACUUM INTO 隐藏临时文件（.tmp-xxxx），成功后 os.replace 成当日备份；
  失败只删临时文件，旧备份原样保留（同日重跑不再有「先删旧」的零备份窗口）。
- 调度：BackupScheduler 在后台循环里按 ``DSH_BACKUP_INTERVAL_SECONDS``（默认 86400，
  0 = 关闭）检查 last_backup_at；失败记录到 /health 并写服务端日志。
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path

from .db import Database
from .health import LAST_BACKUP_AT_KEY, last_backup_at
from .schema import set_meta
from .write_path import now_ms

logger = logging.getLogger("factlayer.backup")


def backup_dir_for(db_path: str) -> Path:
    return Path(db_path).parent / "backups"


def run_backup(db: Database, keep: int = 7) -> str:
    """VACUUM INTO 同目录 backups/backup-YYYYMMDD.db，成功后写 last_backup_at，保留最近 keep 份。

    先写临时文件再原子替换：VACUUM 失败时旧备份不受影响。
    """
    bdir = backup_dir_for(db.path)
    bdir.mkdir(parents=True, exist_ok=True)
    dest = bdir / f"backup-{datetime.now():%Y%m%d}.db"
    tmp = bdir / f".tmp-{uuid.uuid4().hex}.db"
    try:
        db.vacuum_into(str(tmp))
        os.replace(tmp, dest)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            logger.warning("failed to remove temporary backup file %s", tmp)
        raise
    set_meta(db, LAST_BACKUP_AT_KEY, str(now_ms()))
    _prune(bdir, keep)
    logger.info("backup ok: %s（保留最近 %d 份）", dest, keep)
    return str(dest)


class BackupScheduler:
    """按间隔自动备份，并把最近失败状态暴露给 /health。"""

    def __init__(self, db: Database, interval_seconds: int, keep: int = 7) -> None:
        self.db = db
        self.interval_seconds = int(interval_seconds)
        self.keep = keep
        self.last_error: str | None = None
        self.consecutive_failures = 0
        self._state_lock = threading.Lock()
        self._run_lock = threading.Lock()  # 后台循环与手动端点不要同时 VACUUM

    def status(self) -> dict:
        with self._state_lock:
            return {
                "last_backup_error": self.last_error,
                "backup_consecutive_failures": self.consecutive_failures,
            }

    def run_now(self) -> str:
        """立即备份一次（手动端点 / 到期时调用）。失败记录状态、写日志后向上抛。"""
        with self._run_lock:
            try:
                dest = run_backup(self.db, keep=self.keep)
            except BaseException as exc:
                with self._state_lock:
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    self.consecutive_failures += 1
                    failures = self.consecutive_failures
                logger.exception(
                    "backup failed（连续失败 %d 次，已记录到 /health）", failures
                )
                raise
            with self._state_lock:
                self.last_error = None
                self.consecutive_failures = 0
            return dest

    def maybe_backup(self, now: int | None = None) -> str | None:
        """到期才备份；未到期返回 None。interval_seconds <= 0 表示关闭自动备份。"""
        if self.interval_seconds <= 0:
            return None
        current = now_ms() if now is None else int(now)
        last = last_backup_at(self.db)
        if last is not None and current - last < self.interval_seconds * 1000:
            return None
        return self.run_now()


def _prune(bdir: Path, keep: int) -> list[str]:
    """按文件名（含日期）排序去重，只保留最近 keep 份。"""
    files = sorted(
        p for p in bdir.glob("backup-*.db") if p.is_file()
    )
    removed: list[str] = []
    for old in files[:-keep] if keep > 0 else files:
        old.unlink()
        removed.append(str(old))
    if removed:
        logger.info("pruned old backups: %s", removed)
    return removed
