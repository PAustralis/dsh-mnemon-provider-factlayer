"""环境变量加载（全部配置走环境变量，见 .env.example；仓库不含真实地址/密钥）。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(path: str = ".env") -> None:
    """极简 .env 解析：只做 KEY=VALUE 注入，不引入额外依赖。

    已存在的环境变量优先（setdefault），.env 仅作本地默认值。
    """
    p = Path(path)
    if not p.exists():
        return
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Config:
    db_path: str
    falkordb_url: str
    embedding_api_url: str
    embedding_api_key: str
    server_port: int
    backup_keep: int                 # 备份保留份数，默认 7
    derive_loop_enabled: bool        # 是否启动后台派生循环线程
    derive_stub_success: bool        # 派生 stub 是否直接视为成功（本层无真实 processor）


def load_config() -> Config:
    return Config(
        db_path=os.environ.get("DSH_FACT_DB_PATH", "facts.db").strip() or "facts.db",
        falkordb_url=os.environ.get("DSH_FALKORDB_URL", "").strip(),
        embedding_api_url=os.environ.get("DSH_EMBEDDING_API_URL", "").strip(),
        embedding_api_key=os.environ.get("DSH_EMBEDDING_API_KEY", "").strip(),
        server_port=_env_int("DSH_SERVER_PORT", 8000),
        backup_keep=_env_int("DSH_BACKUP_KEEP", 7),
        derive_loop_enabled=_env_bool("DSH_DERIVE_LOOP_ENABLED", False),
        derive_stub_success=_env_bool("DSH_DERIVE_STUB_SUCCESS", True),
    )
