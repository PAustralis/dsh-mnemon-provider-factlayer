"""环境变量加载（全部配置走环境变量，见 .env.example；仓库不含真实地址/密钥）。

第二层（检索层）按 docs/02-检索层设计.md §8 增加配置。环境变量名以设计表为准
（``EMBEDDING_BACKEND`` / ``RETRIEVAL_ROUTES`` …），同时接受历史 ``DSH_`` 前缀别名
（第一层已发布的 ``DSH_EMBEDDING_API_URL`` 等），设计名优先。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

KNOWN_ROUTES = ("vector", "bigram", "trigram", "graph", "recent", "correction")
# §8 默认：trigram 出厂关（v3.1：作融合信号边际低于显著性门槛）；
# recent/correction 是常开的流水线步骤，出现在 RETRIEVAL_ROUTES 里仅作显式声明。
DEFAULT_ROUTES = "vector,bigram"


def _strip_quotes(value: str) -> str:
    """去掉成对的单/双引号：KEY="value" / KEY='value' → value。"""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _load_dotenv(path: str = ".env") -> None:
    """极简 .env 解析：只做 KEY=VALUE 注入，不引入额外依赖。

    已存在的环境变量优先（setdefault），.env 仅作本地默认值。
    值两端成对的引号会去掉（shell 式习惯写法）。
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
        os.environ.setdefault(key.strip(), _strip_quotes(value.strip()))


_load_dotenv()


def _env_raw(*names: str, default: str = "") -> str:
    """按顺序读多个环境变量名（设计名优先，别名兜底）。"""
    for name in names:
        raw = os.environ.get(name)
        if raw is not None and raw.strip() != "":
            return raw.strip()
    return default


def _env_int(*names: str, default: int) -> int:
    raw = _env_raw(*names)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(*names: str, default: float) -> float:
    raw = _env_raw(*names)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(*names: str, default: bool) -> bool:
    raw = _env_raw(*names).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Config:
    db_path: str
    falkordb_url: str
    server_port: int
    backup_keep: int                 # 备份保留份数，默认 7
    backup_interval_seconds: int     # 自动备份检查间隔（秒），默认 86400；0 = 关闭
    derive_loop_enabled: bool        # 是否启动后台派生循环线程
    derive_stub_success: bool        # 派生 stub 是否直接视为成功（未接真实 processor 的 stage）

    # ---- 检索层：R1 向量 ----
    embedding_backend: str = "openai-compat"
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_model: str = ""
    embedding_dim: str = "auto"      # "auto" 或正整数字符串
    embedding_timeout_seconds: float = 30.0

    # ---- 检索层：LLM 生成（本层不实现生成，仅配置快照） ----
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = "deepseek-flash"
    llm_max_tokens: int = 3000

    # ---- 检索层：路线与裁剪 ----
    retrieval_routes: tuple[str, ...] = field(
        default_factory=lambda: ("vector", "bigram")
    )
    mmr_enabled: bool = False
    mmr_lambda: float = 0.7
    rrf_k: int = 60
    route_top_k: int = 50
    final_k: int = 6
    expand_max_items: int = 12
    expand_max_chars: int = 12000
    auto_degrade: bool = True

    @property
    def embedding_dim_int(self) -> int | None:
        from .embeddings import parse_configured_dim

        return parse_configured_dim(self.embedding_dim)

    @property
    def route_names(self) -> tuple[str, ...]:
        """去重保序，只保留 recall 路线（recent/correction 是常开流水线步骤）。"""
        out: list[str] = []
        for name in self.retrieval_routes:
            if name in ("vector", "bigram", "trigram", "graph") and name not in out:
                out.append(name)
        return tuple(out)

    def snapshot(self) -> dict:
        """生效配置快照（/health 用）：模型名/维度/启用路线/开关。"""
        return {
            "embedding_backend": self.embedding_backend,
            "embedding_model": self.embedding_model,
            "embedding_dim": self.embedding_dim,
            "routes": list(self.retrieval_routes),
            "mmr_enabled": self.mmr_enabled,
            "mmr_lambda": self.mmr_lambda,
            "rrf_k": self.rrf_k,
            "route_top_k": self.route_top_k,
            "final_k": self.final_k,
            "expand_max_items": self.expand_max_items,
            "expand_max_chars": self.expand_max_chars,
            "auto_degrade": self.auto_degrade,
            "llm_model": self.llm_model,
            "llm_max_tokens": self.llm_max_tokens,
        }


def parse_routes(raw: str) -> tuple[str, ...]:
    """解析 RETRIEVAL_ROUTES；未知路线名 → 抛 ValueError（启动时暴露）。"""
    names: list[str] = []
    for chunk in (raw or "").split(","):
        name = chunk.strip().lower()
        if not name:
            continue
        if name not in KNOWN_ROUTES:
            raise ValueError(
                f"RETRIEVAL_ROUTES 含未知路线名 {name!r}；"
                f"可用：{', '.join(KNOWN_ROUTES)}"
            )
        if name not in names:
            names.append(name)
    return tuple(names)


def load_config() -> Config:
    raw_routes = _env_raw("RETRIEVAL_ROUTES", "DSH_RETRIEVAL_ROUTES", default=DEFAULT_ROUTES)
    return Config(
        db_path=_env_raw("DSH_FACT_DB_PATH", default="facts.db") or "facts.db",
        falkordb_url=_env_raw("DSH_FALKORDB_URL"),
        server_port=_env_int("DSH_SERVER_PORT", default=8000),
        backup_keep=_env_int("DSH_BACKUP_KEEP", default=7),
        backup_interval_seconds=_env_int("DSH_BACKUP_INTERVAL_SECONDS", default=86400),
        derive_loop_enabled=_env_bool("DSH_DERIVE_LOOP_ENABLED", default=False),
        derive_stub_success=_env_bool("DSH_DERIVE_STUB_SUCCESS", default=True),
        embedding_backend=_env_raw(
            "EMBEDDING_BACKEND", "DSH_EMBEDDING_BACKEND", default="openai-compat"
        ),
        embedding_base_url=_env_raw(
            "EMBEDDING_BASE_URL", "DSH_EMBEDDING_BASE_URL", "DSH_EMBEDDING_API_URL"
        ),
        embedding_api_key=_env_raw(
            "EMBEDDING_API_KEY", "DSH_EMBEDDING_API_KEY", "DSH_EMBEDDING_API_KEY"
        ),
        embedding_model=_env_raw("EMBEDDING_MODEL", "DSH_EMBEDDING_MODEL"),
        embedding_dim=_env_raw("EMBEDDING_DIM", "DSH_EMBEDDING_DIM", default="auto"),
        embedding_timeout_seconds=_env_float(
            "EMBEDDING_TIMEOUT_SECONDS", "DSH_EMBEDDING_TIMEOUT_SECONDS", default=30.0
        ),
        llm_base_url=_env_raw("LLM_BASE_URL", "DSH_LLM_BASE_URL"),
        llm_api_key=_env_raw("LLM_API_KEY", "DSH_LLM_API_KEY"),
        llm_model=_env_raw("LLM_MODEL", "DSH_LLM_MODEL", default="deepseek-flash"),
        llm_max_tokens=_env_int("LLM_MAX_TOKENS", "DSH_LLM_MAX_TOKENS", default=3000),
        retrieval_routes=parse_routes(raw_routes),
        mmr_enabled=_env_bool("MMR_ENABLED", "DSH_MMR_ENABLED", default=False),
        mmr_lambda=_env_float("MMR_LAMBDA", "DSH_MMR_LAMBDA", default=0.7),
        rrf_k=_env_int("RRF_K", "DSH_RRF_K", default=60),
        route_top_k=_env_int("ROUTE_TOP_K", "DSH_ROUTE_TOP_K", default=50),
        final_k=_env_int("FINAL_K", "DSH_FINAL_K", default=6),
        expand_max_items=_env_int(
            "EXPAND_MAX_ITEMS", "DSH_EXPAND_MAX_ITEMS", default=12
        ),
        expand_max_chars=_env_int(
            "EXPAND_MAX_CHARS", "DSH_EXPAND_MAX_CHARS", default=12000
        ),
        auto_degrade=_env_bool(
            "RETRIEVAL_AUTO_DEGRADE", "DSH_RETRIEVAL_AUTO_DEGRADE", default=True
        ),
    )
