"""环境变量加载（全部配置走环境变量，见 .env.example；仓库不含真实地址/密钥）。

第二层（检索层）按 docs/02-检索层设计 v8 §11 收敛：

- 出厂 ``RETRIEVAL_ROUTES=vector``（R1 真实 embedding 于 2026-11-11 重测达标，
  R@6 0.8729；二元组仅作向量不可用时的降级路，见 docs/04）。
- ``FINAL_K=4``、单条 ``ITEM_MAX_CHARS=1200``、总 ``TOTAL_MAX_CHARS=4800``
  （contract strict-v1 默认 maxRankedResults=4；要 6 条需同时把策略配到 ≥6）。
- 会话展开默认关；开启时只能占用 4 条名额内。
- ``ADMIN_TOKEN`` 为空时 admin 端点仅接受回环请求。

环境变量名以设计表为准（``EMBEDDING_BACKEND`` / ``RETRIEVAL_ROUTES`` …），同时接受
历史 ``DSH_`` 前缀别名（第一层已发布的 ``DSH_EMBEDDING_API_URL`` 等），设计名优先。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("factlayer.config")

KNOWN_ROUTES = ("vector", "bigram", "trigram", "graph")
# 2026-11-11 真实 embedding 重测：向量单路 R@6=0.8729（bigram 0.4875，等权融合
# 0.6833 反而更差），因此出厂默认 vector 单路；bigram 只在向量不可用时降级。
DEFAULT_ROUTES = "vector"

# 契约常量（v8 §5，P0）：provider-native → strict-v1 maxRankedResults 默认 4
FINAL_K_DEFAULT = 4
ITEM_MAX_CHARS_DEFAULT = 1200
TOTAL_MAX_CHARS_DEFAULT = 4800
SCORE_SEMANTICS = "provider-native"


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


def _env_non_negative_int(*names: str, default: int) -> int:
    """读整数并要求 ≥ 0；负数记 warning 后回退默认值（与 ``db.py`` 同口径）。

    ``DSH_WAL_AUTOCHECKPOINT_PAGES`` 有两个读取点：``Database`` 构造时自己读一次
    环境变量，``load_config`` 又读一次并显式传进 ``Database``。两处语义必须一致，
    否则同一个变量会出现「config 快照说负数、实际连接回退 1000」的分裂。
    """
    value = _env_int(*names, default=default)
    if value < 0:
        logger.warning(
            "%s=%r 不能为负，回退默认 %d 页（与 Database 行为一致）",
            names[0],
            _env_raw(*names),
            default,
        )
        return default
    return value


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

    # ---- SQLite 运维（2026-11-12） ----
    wal_autocheckpoint_pages: int = 1000   # PRAGMA wal_autocheckpoint（页）
    wal_checkpoint_interval_seconds: int = 300  # 后台 TRUNCATE 最小间隔；0 = 关闭
    wal_checkpoint_idle_seconds: float = 60.0   # 距上次写入多久才算「无写入空闲」
    fts_optimize_interval_seconds: int = 86400  # 后台 FTS5 optimize 间隔；0 = 关闭
    derive_batch_size: int = 500                # 派生重建 executemany 每批行数

    # ---- 检索层：R1 向量 ----
    embedding_backend: str = "openai-compat"
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_model: str = ""
    embedding_dim: str = "auto"      # "auto" 或正整数字符串
    embedding_timeout_seconds: float = 3.0
    embedding_trust_env: bool = False  # True 才让 httpx 读取本机代理环境变量
    embedding_max_spend_usd: float = 0.50

    # ---- 检索层：LLM 生成（本层不实现生成，仅配置快照） ----
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = "deepseek-flash"
    llm_max_tokens: int = 3000

    # ---- 检索层：路线与裁剪（v8 §5/§11） ----
    retrieval_routes: tuple[str, ...] = field(default_factory=lambda: ("bigram",))
    rrf_k: int = 60
    route_top_k: int = 50
    final_k: int = FINAL_K_DEFAULT
    item_max_chars: int = ITEM_MAX_CHARS_DEFAULT
    total_max_chars: int = TOTAL_MAX_CHARS_DEFAULT
    expand_enabled: bool = False
    expand_window: int = 1
    expand_max_items: int = FINAL_K_DEFAULT
    weak_best_rank: int = 3
    # v8 §12：明确不做「运行时质量自动降级」。默认关闭；保留开关仅用于离线评测
    # （research/recall/baseline_r2.py --record-meta 写 meta:retrieval:eval）复现实验。
    auto_degrade: bool = False

    # ---- 检索层：运维 ----
    admin_token: str = ""
    canary_enabled: bool = True
    canary_size: int = 20
    canary_alert_rate: float = 0.20

    # ---- 对接层（docs/06 v3） ----
    # provider discover 的固定空间标识；remember 的 session_id 也用同一个值。
    factlayer_space_id: str = "factlayer-default"
    # /health 后台快照刷新间隔（秒）；请求路径只读缓存，绝不查询聚合。
    health_refresh_seconds: float = 5.0

    @property
    def embedding_configured(self) -> bool:
        """embedding 是否「配置了」（不检查可用性；可用性由 build_provider 暴露）。"""
        if not self.embedding_model.strip():
            return False
        if self.embedding_backend == "openai-compat" and not self.embedding_base_url.strip():
            return False
        return True

    @property
    def embedding_dim_int(self) -> int | None:
        from .embeddings import parse_configured_dim

        return parse_configured_dim(self.embedding_dim)

    @property
    def route_names(self) -> tuple[str, ...]:
        """去重保序，只保留 recall 路线。"""
        out: list[str] = []
        for name in self.retrieval_routes:
            if name in KNOWN_ROUTES and name not in out:
                out.append(name)
        return tuple(out)

    def snapshot(self) -> dict:
        """生效配置快照（/health 用）：模型名/维度/启用路线/契约常量/开关。"""
        return {
            "embedding_backend": self.embedding_backend,
            "embedding_model": self.embedding_model,
            "embedding_dim": self.embedding_dim,
            "embedding_trust_env": self.embedding_trust_env,
            "derive_loop_enabled": self.derive_loop_enabled,
            "routes": list(self.retrieval_routes),
            "rrf_k": self.rrf_k,
            "route_top_k": self.route_top_k,
            "final_k": self.final_k,
            "item_max_chars": self.item_max_chars,
            "total_max_chars": self.total_max_chars,
            "expand_enabled": self.expand_enabled,
            "expand_window": self.expand_window,
            "expand_max_items": self.expand_max_items,
            "weak_best_rank": self.weak_best_rank,
            "auto_degrade": self.auto_degrade,
            "score_semantics": SCORE_SEMANTICS,
            "wal_autocheckpoint_pages": self.wal_autocheckpoint_pages,
            "wal_checkpoint_interval_seconds": self.wal_checkpoint_interval_seconds,
            "wal_checkpoint_idle_seconds": self.wal_checkpoint_idle_seconds,
            "fts_optimize_interval_seconds": self.fts_optimize_interval_seconds,
            "derive_batch_size": self.derive_batch_size,
            "admin_token_set": bool(self.admin_token),
            "canary_enabled": self.canary_enabled,
            "canary_size": self.canary_size,
            "canary_alert_rate": self.canary_alert_rate,
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
    raw_routes = _env_raw(
        "RETRIEVAL_ROUTES", "DSH_RETRIEVAL_ROUTES", default=DEFAULT_ROUTES
    )
    routes = parse_routes(raw_routes)
    embedding_backend = _env_raw(
        "EMBEDDING_BACKEND", "DSH_EMBEDDING_BACKEND", default="openai-compat"
    )
    embedding_base_url = _env_raw(
        "EMBEDDING_BASE_URL", "DSH_EMBEDDING_BASE_URL", "DSH_EMBEDDING_API_URL"
    )
    embedding_model = _env_raw("EMBEDDING_MODEL", "DSH_EMBEDDING_MODEL")
    # 「vector 已启用 + embedding 已配置」时派生循环默认开：否则写入后 embedding
    # 永久 pending，默认向量路线永久降级（2026-11-12 联调实测 P0）。显式
    # DSH_DERIVE_LOOP_ENABLED=0 才关闭。
    loop_default = "vector" in routes and bool(embedding_model.strip()) and (
        embedding_backend != "openai-compat" or bool(embedding_base_url.strip())
    )
    return Config(
        db_path=_env_raw("DSH_FACT_DB_PATH", default="facts.db") or "facts.db",
        falkordb_url=_env_raw("DSH_FALKORDB_URL"),
        server_port=_env_int("DSH_SERVER_PORT", default=8000),
        backup_keep=_env_int("DSH_BACKUP_KEEP", default=7),
        backup_interval_seconds=_env_int("DSH_BACKUP_INTERVAL_SECONDS", default=86400),
        derive_loop_enabled=_env_bool("DSH_DERIVE_LOOP_ENABLED", default=loop_default),
        derive_stub_success=_env_bool("DSH_DERIVE_STUB_SUCCESS", default=True),
        wal_autocheckpoint_pages=_env_non_negative_int(
            "DSH_WAL_AUTOCHECKPOINT_PAGES", default=1000
        ),
        wal_checkpoint_interval_seconds=_env_int(
            "DSH_WAL_CHECKPOINT_INTERVAL_SECONDS", default=300
        ),
        wal_checkpoint_idle_seconds=_env_float(
            "DSH_WAL_CHECKPOINT_IDLE_SECONDS", default=60.0
        ),
        fts_optimize_interval_seconds=_env_int(
            "DSH_FTS_OPTIMIZE_INTERVAL_SECONDS", default=86400
        ),
        derive_batch_size=_env_int("DSH_DERIVE_BATCH_SIZE", default=500),
        embedding_backend=embedding_backend,
        embedding_base_url=embedding_base_url,
        embedding_api_key=_env_raw(
            "EMBEDDING_API_KEY", "DSH_EMBEDDING_API_KEY", "DSH_EMBEDDING_API_KEY"
        ),
        embedding_model=embedding_model,
        embedding_dim=_env_raw("EMBEDDING_DIM", "DSH_EMBEDDING_DIM", default="auto"),
        embedding_timeout_seconds=_env_float(
            "EMBEDDING_TIMEOUT_SECONDS", "DSH_EMBEDDING_TIMEOUT_SECONDS", default=3.0
        ),
        embedding_trust_env=_env_bool(
            "EMBEDDING_TRUST_ENV", "DSH_EMBEDDING_TRUST_ENV", default=False
        ),
        embedding_max_spend_usd=_env_float(
            "EMBEDDING_MAX_SPEND_USD", "DSH_EMBEDDING_MAX_SPEND_USD", default=0.50
        ),
        llm_base_url=_env_raw("LLM_BASE_URL", "DSH_LLM_BASE_URL"),
        llm_api_key=_env_raw("LLM_API_KEY", "DSH_LLM_API_KEY"),
        llm_model=_env_raw("LLM_MODEL", "DSH_LLM_MODEL", default="deepseek-flash"),
        llm_max_tokens=_env_int("LLM_MAX_TOKENS", "DSH_LLM_MAX_TOKENS", default=3000),
        retrieval_routes=routes,
        rrf_k=_env_int("RRF_K", "DSH_RRF_K", default=60),
        route_top_k=_env_int("ROUTE_TOP_K", "DSH_ROUTE_TOP_K", default=50),
        final_k=_env_int("FINAL_K", "DSH_FINAL_K", default=FINAL_K_DEFAULT),
        item_max_chars=_env_int(
            "ITEM_MAX_CHARS", "DSH_ITEM_MAX_CHARS", default=ITEM_MAX_CHARS_DEFAULT
        ),
        total_max_chars=_env_int(
            "TOTAL_MAX_CHARS", "DSH_TOTAL_MAX_CHARS", default=TOTAL_MAX_CHARS_DEFAULT
        ),
        expand_enabled=_env_bool(
            "EXPAND_ENABLED", "EXPAND_SESSION", "DSH_EXPAND_ENABLED", default=False
        ),
        expand_window=_env_int("EXPAND_WINDOW", "DSH_EXPAND_WINDOW", default=1),
        expand_max_items=_env_int(
            "EXPAND_MAX_ITEMS", "DSH_EXPAND_MAX_ITEMS", default=FINAL_K_DEFAULT
        ),
        weak_best_rank=_env_int("WEAK_BEST_RANK", "DSH_WEAK_BEST_RANK", default=3),
        auto_degrade=_env_bool(
            "RETRIEVAL_AUTO_DEGRADE", "DSH_RETRIEVAL_AUTO_DEGRADE", default=False
        ),
        admin_token=_env_raw("ADMIN_TOKEN", "DSH_ADMIN_TOKEN"),
        canary_enabled=_env_bool("CANARY_ENABLED", "DSH_CANARY_ENABLED", default=True),
        canary_size=_env_int("CANARY_SIZE", "DSH_CANARY_SIZE", default=20),
        canary_alert_rate=_env_float(
            "CANARY_ALERT_RATE", "DSH_CANARY_ALERT_RATE", default=0.20
        ),
        factlayer_space_id=_env_raw(
            "FACTLAYER_SPACE_ID", "DSH_FACTLAYER_SPACE_ID", default="factlayer-default"
        )
        or "factlayer-default",
        health_refresh_seconds=_env_float(
            "HEALTH_REFRESH_SECONDS", "DSH_HEALTH_REFRESH_SECONDS", default=5.0
        ),
    )
