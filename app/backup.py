"""备份：VACUUM INTO backups/backup-YYYYMMDD.db（WAL 下直接拷 .db 会漏 -wal 数据）。"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from .db import Database
from .health import LAST_BACKUP_AT_KEY
from .schema import set_meta
from .write_path import now_ms

logger = logging.getLogger("factlayer.backup")


def backup_dir_for(db_path: str) -> Path:
    return Path(db_path).parent / "backups"


def run_backup(db: Database, keep: int = 7) -> str:
    """VACUUM INTO 同目录 backups/backup-YYYYMMDD.db，成功后写 last_backup_at，保留最近 keep 份。"""
    bdir = backup_dir_for(db.path)
    bdir.mkdir(parents=True, exist_ok=True)
    dest = bdir / f"backup-{datetime.now():%Y%m%d}.db"
    if dest.exists():
        dest.unlink()  # 同日重跑：VACUUM INTO 要求目标不存在，先删旧再生成
    db.vacuum_into(str(dest))
    set_meta(db, LAST_BACKUP_AT_KEY, str(now_ms()))
    _prune(bdir, keep)
    logger.info("backup ok: %s（保留最近 %d 份）", dest, keep)
    return str(dest)


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
