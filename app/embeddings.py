"""R1 向量：embedding provider 接口 + 维度指纹 + float32 归一化存储 + 内存暴力点积。

设计要点（docs/02 检索层 v8 §7）：

- 后端只认接口：``openai-compat``（httpx 调 OpenAI 兼容 /embeddings）或
  ``local-onnx``（骨架；依赖 import 失败时**启动即报明确错误**，绝不静默）。
- ``EMBEDDING_DIM=auto``：首次调用取 ``len(vector)`` 写入 meta，之后每次校验一致。
- **按行存 ``model_id + dim``**；无 embedding 行天然被排除在 R1 候选外（JOIN memories）。
- **写事务提交后增量刷新内存矩阵**（新增/替换该行向量），不整表重载；
  启动/重建时全量载入；矩阵与 DB 用 ``gseq`` 对齐（恢复旧备份后 sqlite_sequence
  可能回退，因此恢复后必须全量重载）。
- ``model_id + dim`` 与库内不符 → 抛 :class:`EmbeddingModelMismatch`，由 API 层
  拒绝检索（HTTP 409）并提示 ``POST /admin/rebuild``；**不自动重建**。
- 写库前 L2 归一化，float32 小端 BLOB；检索时内存矩阵暴力点积（点积即余弦）。
"""

from __future__ import annotations

import bisect
import logging
import re
import sqlite3
import threading
import time
from typing import Callable, Protocol, runtime_checkable

import numpy as np

from .db import Database

logger = logging.getLogger("factlayer.embeddings")

EMBEDDING_MODEL_KEY = "embedding_model"
EMBEDDING_DIM_KEY = "embedding_dim"

SUPPORTED_BACKENDS = ("openai-compat", "local-onnx")


class EmbeddingConfigError(RuntimeError):
    """配置/依赖问题：必须在启动时暴露，不允许拖到查询时。"""


class EmbeddingModelMismatch(RuntimeError):
    """库内向量与当前 ``model_id + dim`` 不符 → 拒绝检索，提示重建。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class EmbeddingRequestError(RuntimeError):
    """embedding 请求/传输失败（含客户端构造失败）：可计数、可重试。

    与 :class:`EmbeddingConfigError` 区分：配置错误必须启动即暴露，请求错误是
    运行期故障，由派生循环计数、退避、熔断，并在 ``/health`` 暴露。
    """


# 与检索层向量路同一熔断语义（连续 3 次失败 → 打开 5 分钟），两边共用实现。
CIRCUIT_FAILURE_THRESHOLD = 3
CIRCUIT_OPEN_SECONDS = 300.0

_SECRET_PATTERNS = (
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)\b(?:api[_-]?key|token|secret)\s*[=:]\s*\S+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{6,}"),
)


def redact_error(exc: BaseException, *, max_chars: int = 500) -> str:
    """``类型: 消息`` 的脱敏短串（去掉 Bearer/API key，长度封顶）。"""
    text = f"{type(exc).__name__}: {exc}"
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("<redacted>", text)
    return text[:max_chars]


class CircuitBreaker:
    """连续失败 N 次 → 打开 M 秒；期间拒绝调用（向量路与派生 embedding 共用）。"""

    def __init__(
        self,
        *,
        threshold: int = CIRCUIT_FAILURE_THRESHOLD,
        open_seconds: float = CIRCUIT_OPEN_SECONDS,
        label: str = "embedding",
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.threshold = int(threshold)
        self.open_seconds = float(open_seconds)
        self.label = label
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._failures = 0
        self._open_until = 0.0
        self._last_error: str | None = None

    def allow(self) -> bool:
        with self._lock:
            return self._clock() >= self._open_until

    def record_failure(self, exc: BaseException) -> bool:
        """记一次失败；返回本次是否（重新）打开了熔断。"""
        message = redact_error(exc)
        with self._lock:
            self._failures += 1
            self._last_error = message
            opened = self._failures >= self.threshold
            if opened:
                self._open_until = self._clock() + self.open_seconds
            failures = self._failures
        if opened:
            logger.error(
                "%s 连续失败 %d 次，熔断 %.0fs：%s",
                self.label,
                failures,
                self.open_seconds,
                message,
            )
        return opened

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._last_error = None

    def retry_after(self) -> float:
        with self._lock:
            return max(0.0, self._open_until - self._clock())

    def state(self) -> dict:
        with self._lock:
            remaining = max(0.0, self._open_until - self._clock())
            return {
                "circuit_open": remaining > 0,
                "consecutive_failures": self._failures,
                "open_seconds_remaining": remaining,
                "last_error": self._last_error,
            }


@runtime_checkable
class EmbeddingProvider(Protocol):
    """可注入的 embedding 接口（测试用确定性假实现，不联网）。"""

    model_id: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class OpenAICompatProvider:
    """OpenAI 兼容 ``POST {base_url}/embeddings`` 后端（httpx 同步调用）。

    ``trust_env=False``（默认）**直连** embedding 端点：本机 ``NO_PROXY`` /
    ``HTTP_PROXY`` 里的畸形条目（如 ``[::1]``）会在 httpx 解析代理时抛
    ``InvalidURL``，把请求死在客户端构造阶段。密钥请求也不应被用户机器上的
    代理截获；确实需要走代理时才配置 ``EMBEDDING_TRUST_ENV=1``。
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model_id: str,
        *,
        timeout_seconds: float = 30.0,
        trust_env: bool = False,
    ) -> None:
        if not base_url:
            raise EmbeddingConfigError(
                "EMBEDDING_BACKEND=openai-compat 需要 EMBEDDING_BASE_URL"
            )
        if not model_id:
            raise EmbeddingConfigError("EMBEDDING_MODEL 不能为空")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model_id = model_id
        self.timeout_seconds = float(timeout_seconds)
        self.trust_env = bool(trust_env)

    @property
    def endpoint(self) -> str:
        return self.base_url + "/embeddings"

    def _make_client(self):
        """构造 httpx 客户端；代理环境解析失败 → 分类错误，不裸 traceback。"""
        import httpx

        try:
            return httpx.Client(
                timeout=self.timeout_seconds, trust_env=self.trust_env
            )
        except Exception as exc:
            raise EmbeddingRequestError(
                "embedding HTTP client 初始化失败"
                f"（EMBEDDING_TRUST_ENV={'true' if self.trust_env else 'false'}）："
                f"{type(exc).__name__}: {exc}。"
                "若本机代理环境变量（NO_PROXY/HTTP_PROXY）畸形，请保持"
                " EMBEDDING_TRUST_ENV=0（默认直连）"
            ) from exc

    def embed(self, texts: list[str]) -> list[list[float]]:
        client = self._make_client()
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            try:
                response = client.post(
                    self.endpoint,
                    headers=headers,
                    json={"model": self.model_id, "input": list(texts)},
                )
                response.raise_for_status()
            except Exception as exc:
                # 传输/HTTP/URL 错误统一分类（脱敏后由派生层计数与 /health 暴露）。
                raise EmbeddingRequestError(
                    f"embedding 请求失败：{redact_error(exc)}"
                ) from exc
        finally:
            client.close()
        payload = response.json()
        data = payload.get("data")
        if not isinstance(data, list) or not data:
            raise RuntimeError("embedding 响应缺少 data 数组")
        data = sorted(data, key=lambda item: item.get("index", 0))
        vectors = [item["embedding"] for item in data]
        if len(vectors) != len(texts):
            raise RuntimeError(
                f"embedding 响应条数不符：请求 {len(texts)} 条，返回 {len(vectors)} 条"
            )
        lengths = {len(v) for v in vectors}
        if len(lengths) != 1 or 0 in lengths:
            raise RuntimeError(f"embedding 响应维度不一致：{sorted(lengths)}")
        return vectors


class LocalOnnxProvider:
    """local-onnx 骨架：依赖缺失/模型缺失必须在启动时报明确错误。

    本层不实现推理（设计接受「骨架」定位）；一旦 import 成功但仍未接模型加载，
    构造时抛出 :class:`EmbeddingConfigError`，保证不会静默返回空向量。
    """

    is_skeleton = True

    def __init__(self, model_id: str) -> None:
        if not model_id:
            raise EmbeddingConfigError("EMBEDDING_MODEL 不能为空")
        self.model_id = model_id
        try:
            import onnxruntime  # noqa: F401
            import tokenizers  # noqa: F401
        except ImportError as exc:  # 依赖缺失 → 启动报错，绝不静默
            raise EmbeddingConfigError(
                "EMBEDDING_BACKEND=local-onnx 不可用：缺少本地推理依赖"
                "（onnxruntime / tokenizers）。请安装依赖，或改用"
                " EMBEDDING_BACKEND=openai-compat / 关闭 vector 路线。"
            ) from exc
        raise EmbeddingConfigError(
            "EMBEDDING_BACKEND=local-onnx 目前只是骨架：onnxruntime 已安装但模型加载"
            "尚未实现。请改用 EMBEDDING_BACKEND=openai-compat，或关闭 vector 路线。"
        )

    def embed(self, texts: list[str]) -> list[list[float]]:  # pragma: no cover
        raise EmbeddingConfigError("local-onnx 后端尚未实现")


def build_provider(cfg) -> EmbeddingProvider:
    """按配置构造 provider；配置错误在启动时抛错（不拖到查询时）。"""
    backend = cfg.embedding_backend
    if backend == "openai-compat":
        return OpenAICompatProvider(
            base_url=cfg.embedding_base_url,
            api_key=cfg.embedding_api_key,
            model_id=cfg.embedding_model,
            timeout_seconds=cfg.embedding_timeout_seconds,
            trust_env=getattr(cfg, "embedding_trust_env", False),
        )
    if backend == "local-onnx":
        return LocalOnnxProvider(cfg.embedding_model)
    raise EmbeddingConfigError(
        f"未知 EMBEDDING_BACKEND={backend!r}，支持：{', '.join(SUPPORTED_BACKENDS)}"
    )


# ---------------------------------------------------------------- 维度 / 指纹

def parse_configured_dim(raw: str) -> int | None:
    """``auto`` → None；否则返回正整数维度。非法值抛 EmbeddingConfigError。"""
    text = (raw or "").strip().lower()
    if text in ("", "auto"):
        return None
    try:
        dim = int(text)
    except ValueError as exc:
        raise EmbeddingConfigError(
            f"EMBEDDING_DIM={raw!r} 非法：只接受 auto 或正整数"
        ) from exc
    if dim <= 0:
        raise EmbeddingConfigError(f"EMBEDDING_DIM={raw!r} 必须为正整数")
    return dim


def _read_fingerprint(conn: sqlite3.Connection) -> tuple[str | None, int | None]:
    rows = conn.execute(
        "SELECT key, value FROM meta WHERE key IN (?, ?)",
        (EMBEDDING_MODEL_KEY, EMBEDDING_DIM_KEY),
    ).fetchall()
    values = {row[0]: row[1] for row in rows}
    model = values.get(EMBEDDING_MODEL_KEY)
    raw_dim = values.get(EMBEDDING_DIM_KEY)
    try:
        dim = int(raw_dim) if raw_dim is not None else None
    except ValueError:
        dim = None
    return model, dim


def _write_fingerprint(
    conn: sqlite3.Connection, model_id: str, dim: int, configured_dim: int | None
) -> None:
    if configured_dim is not None and configured_dim != dim:
        raise EmbeddingModelMismatch(
            f"EMBEDDING_DIM={configured_dim} 与模型输出维度 {dim} 不符"
            f"（EMBEDDING_MODEL={model_id}）"
        )
    stored_model, stored_dim = _read_fingerprint(conn)
    if stored_model is None and stored_dim is None:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (EMBEDDING_MODEL_KEY, model_id),
        )
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (EMBEDDING_DIM_KEY, str(dim)),
        )
        logger.info("embedding 指纹首次落库：model=%s dim=%d", model_id, dim)
        return
    _assert_matches(stored_model, stored_dim, model_id, dim, configured_dim)


def _assert_matches(
    stored_model: str | None,
    stored_dim: int | None,
    model_id: str,
    dim: int,
    configured_dim: int | None,
) -> None:
    if configured_dim is not None and dim != configured_dim:
        raise EmbeddingModelMismatch(
            f"EMBEDDING_DIM={configured_dim} 与模型输出维度 {dim} 不符"
            f"（EMBEDDING_MODEL={model_id}）"
        )
    if stored_model is not None and stored_model != model_id:
        raise EmbeddingModelMismatch(
            f"库内向量由模型 {stored_model!r} 生成，当前配置为 {model_id!r}；"
            "拒绝检索以免新旧向量混存。请执行 POST /admin/rebuild 后重试。"
        )
    if stored_dim is not None and stored_dim != dim:
        raise EmbeddingModelMismatch(
            f"库内向量维度 {stored_dim}，当前模型输出维度 {dim}；"
            "拒绝检索以免新旧向量混存。请执行 POST /admin/rebuild 后重试。"
        )


def write_embedding_row(
    conn: sqlite3.Connection,
    memory_id: str,
    vector: list[float],
    *,
    model_id: str,
    configured_dim: int | None,
) -> tuple[int, int]:
    """在调用方事务内写入一行向量 + 指纹；返回 (dim, gseq)。"""
    dim = len(vector)
    if dim == 0:
        raise RuntimeError("embedding 向量为空")
    packed = pack_vector(vector)
    _write_fingerprint(conn, model_id, dim, configured_dim)
    conn.execute(
        "INSERT OR REPLACE INTO embeddings(memory_id, vector, model_id, dim) "
        "VALUES (?, ?, ?, ?)",
        (memory_id, packed, model_id, dim),
    )
    row = conn.execute("SELECT gseq FROM memories WHERE id=?", (memory_id,)).fetchone()
    return dim, int(row[0]) if row is not None else 0


def store_embedding(
    db: Database,
    memory_id: str,
    vector: list[float],
    *,
    model_id: str,
    configured_dim: int | None,
    index: "VectorIndex | None" = None,
) -> None:
    """L2 归一化 + float32 小端入库（按行存 model_id/dim），同事务维护指纹。

    提交后**增量刷新**内存向量矩阵（``index`` 非空时），不整表重载。
    """
    with db.write_txn() as conn:
        dim, gseq = write_embedding_row(
            conn,
            memory_id,
            vector,
            model_id=model_id,
            configured_dim=configured_dim,
        )
    if index is not None and gseq:
        index.upsert(memory_id, gseq, vector, model_id=model_id, dim=dim)


def check_fingerprint(
    db: Database, provider: EmbeddingProvider, configured_dim: int | None
) -> tuple[str | None, int | None]:
    """检索前校验库内指纹；不符抛 :class:`EmbeddingModelMismatch`。"""
    conn = db.read_conn
    stored_model, stored_dim = _read_fingerprint(conn)
    vector_dim = _first_vector_dim(conn)
    if vector_dim is not None:
        _assert_matches(
            stored_model, stored_dim, provider.model_id, vector_dim, configured_dim
        )
    elif configured_dim is not None and stored_dim is not None:
        _assert_matches(
            stored_model, stored_dim, provider.model_id, configured_dim, configured_dim
        )
    # 按行校验 model_id / dim：任何一行不匹配都是配置事故（不自动重建）
    rows = conn.execute(
        "SELECT DISTINCT model_id, dim FROM embeddings LIMIT 50"
    ).fetchall()
    for row in rows:
        row_dim = int(row[1])
        if vector_dim is not None and row_dim and row_dim != vector_dim:
            raise EmbeddingModelMismatch(
                f"库内向量维度不一致：{row_dim} vs {vector_dim}；"
                "请执行 POST /admin/rebuild 后重试。"
            )
        _assert_matches(
            stored_model, stored_dim, provider.model_id, row_dim or (vector_dim or 0), configured_dim
        )
        if row[0] and row[0] != provider.model_id:
            raise EmbeddingModelMismatch(
                f"库内存在由模型 {row[0]!r} 生成的向量行，当前配置为 "
                f"{provider.model_id!r}；请执行 POST /admin/rebuild 后重试。"
            )
    return stored_model, stored_dim


def _first_vector_dim(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT dim FROM embeddings LIMIT 1").fetchone()
    if row is not None and int(row[0]) > 0:
        return int(row[0])
    row = conn.execute("SELECT vector FROM embeddings LIMIT 1").fetchone()
    if row is None:
        return None
    blob = row[0]
    if not blob or len(blob) % 4:
        return None
    return len(blob) // 4


# ---------------------------------------------------------------- 向量打包 / 索引

def normalize_vector(vector) -> np.ndarray:
    """L2 归一化（零向量原样返回）。"""
    arr = np.asarray(vector, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(arr))
    if norm > 0:
        arr = arr / norm
    return arr.astype(np.float32, copy=False)


def pack_vector(vector) -> bytes:
    """float32 小端 BLOB（写入前 L2 归一化，点积即余弦）。"""
    return np.asarray(normalize_vector(vector), dtype="<f4").tobytes()


def unpack_vector(blob: bytes, dim: int | None = None) -> np.ndarray:
    arr = np.frombuffer(blob, dtype="<f4")
    if dim is not None and arr.size != dim:
        raise EmbeddingModelMismatch(
            f"库内向量维度 {arr.size} 与期望 {dim} 不符；请 POST /admin/rebuild"
        )
    return arr.astype(np.float32, copy=False)


class VectorIndex:
    """内存矩阵（只回 id + 距离），gseq 对齐、写事务后增量 upsert。

    1024 维 × 1 万条 ≈ 40MB，暴力点积 ms 级（§7）。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._fingerprint: tuple[str | None, int | None] | None = None
        self._ids: list[str] = []
        self._gseqs: list[int] = []
        self._vectors: list[np.ndarray] = []
        self._matrix: np.ndarray | None = None
        self._loaded_key: tuple[int, int, str, int] | None = None

    def invalidate(self) -> None:
        with self._lock:
            self._fingerprint = None
            self._ids = []
            self._gseqs = []
            self._vectors = []
            self._matrix = None
            self._loaded_key = None

    def load(
        self,
        db: Database,
        *,
        model_id: str | None = None,
        dim: int | None = None,
    ) -> tuple[tuple[str, ...], np.ndarray, tuple[int, ...]]:
        """返回 (ids, matrix, gseqs)。只包含 embeddings 行（无向量行天然排除）。"""
        conn = db.read_conn
        where = ""
        params: list[object] = []
        if model_id is not None and dim is not None:
            where = " WHERE e.model_id=? AND e.dim=?"
            params = [model_id, int(dim)]
        elif model_id is not None:
            where = " WHERE e.model_id=?"
            params = [model_id]
        count_row = conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(m.gseq), 0) FROM embeddings e "
            "JOIN memories m ON m.id = e.memory_id"
            + where,
            params,
        ).fetchone()
        key = (int(count_row[0]), int(count_row[1]), model_id or "", int(dim or 0))
        with self._lock:
            if key == self._loaded_key and self._matrix is not None:
                return tuple(self._ids), self._matrix, tuple(self._gseqs)
        rows = conn.execute(
            "SELECT e.memory_id, e.vector, e.dim, m.gseq FROM embeddings e "
            "JOIN memories m ON m.id = e.memory_id"
            + where
            + " ORDER BY m.gseq ASC",
            params,
        ).fetchall()
        ids: list[str] = []
        gseqs: list[int] = []
        vectors: list[np.ndarray] = []
        dims = {len(row[1]) // 4 for row in rows if row[1]}
        if len(dims) > 1:
            raise EmbeddingModelMismatch(
                f"库内向量维度不一致：{sorted(dims)}；请执行 POST /admin/rebuild"
            )
        for row in rows:
            row_dim = int(row[2]) or (len(row[1]) // 4)
            ids.append(row[0])
            gseqs.append(int(row[3]))
            vectors.append(unpack_vector(row[1], row_dim))
        matrix = (
            np.vstack(vectors).astype(np.float32, copy=False)
            if vectors
            else np.zeros((0, 0), dtype=np.float32)
        )
        with self._lock:
            self._ids = ids
            self._gseqs = gseqs
            self._vectors = vectors
            self._matrix = matrix
            self._loaded_key = key
        return tuple(ids), matrix, tuple(gseqs)

    def upsert(
        self,
        memory_id: str,
        gseq: int,
        vector: list[float] | np.ndarray,
        *,
        model_id: str,
        dim: int,
    ) -> None:
        """写事务提交后增量刷新一行（保持 gseq 升序，矩阵随之更新）。"""
        vec = unpack_vector(pack_vector(vector), dim)
        with self._lock:
            if self._matrix is None:
                # 尚未载入：不猜现有行，等下一次 load 全量载入
                self._loaded_key = None
                return
            index = None
            if memory_id in self._ids:
                index = self._ids.index(memory_id)
                self._vectors[index] = vec
                self._gseqs[index] = int(gseq)
                if self._vectors:
                    self._matrix = np.vstack(self._vectors).astype(
                        np.float32, copy=False
                    )
                return
            position = bisect.bisect_left(self._gseqs, int(gseq))
            self._ids.insert(position, memory_id)
            self._gseqs.insert(position, int(gseq))
            self._vectors.insert(position, vec)
            if self._matrix.size == 0:
                self._matrix = vec.reshape(1, -1)
            else:
                self._matrix = np.insert(
                    self._matrix, position, vec.reshape(1, -1), axis=0
                ).astype(np.float32, copy=False)
            # 增量后更新缓存键（只在该行与已载入矩阵的 model/dim 一致时）
            old = self._loaded_key
            if old is not None and old[2] == model_id and old[3] == dim:
                self._loaded_key = (
                    len(self._ids),
                    max(self._gseqs) if self._gseqs else 0,
                    model_id,
                    dim,
                )
            else:
                self._loaded_key = None


def load_vectors(db: Database) -> list[tuple[str, int, str, int, bytes]]:
    """诊断/重建用：列出所有向量行的 (memory_id, gseq, model_id, dim, blob)。"""
    rows = db.read_conn.execute(
        "SELECT e.memory_id, m.gseq, e.model_id, e.dim, e.vector "
        "FROM embeddings e JOIN memories m ON m.id=e.memory_id ORDER BY m.gseq"
    ).fetchall()
    return [(r[0], int(r[1]), r[2], int(r[3]), r[4]) for r in rows]
