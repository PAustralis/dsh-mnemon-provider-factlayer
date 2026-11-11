"""R1 向量：embedding provider 接口 + 维度指纹 + float32 归一化存储 + 内存暴力点积。

设计要点（docs/02 §3.2 / §8）：

- 后端只认接口：``openai-compat``（httpx 调 OpenAI 兼容 /embeddings）或
  ``local-onnx``（骨架；依赖 import 失败时**启动即报明确错误**，绝不静默）。
- ``EMBEDDING_DIM=auto``：首次调用取 ``len(vector)`` 写入 meta，之后每次校验一致。
- ``model_id + dim`` 与 meta 不符 → 抛 :class:`EmbeddingModelMismatch`，由 API 层
  拒绝检索（HTTP 409）并提示 ``POST /admin/rebuild``；**不自动重建**。
- 写库前 L2 归一化，float32 小端 BLOB；检索时内存矩阵暴力点积（点积即余弦）。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from typing import Protocol, runtime_checkable

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


@runtime_checkable
class EmbeddingProvider(Protocol):
    """可注入的 embedding 接口（测试用确定性假实现，不联网）。"""

    model_id: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class OpenAICompatProvider:
    """OpenAI 兼容 ``POST {base_url}/embeddings`` 后端（httpx 同步调用）。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model_id: str,
        *,
        timeout_seconds: float = 30.0,
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

    @property
    def endpoint(self) -> str:
        return self.base_url + "/embeddings"

    def embed(self, texts: list[str]) -> list[list[float]]:
        import httpx

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        response = httpx.post(
            self.endpoint,
            headers=headers,
            json={"model": self.model_id, "input": list(texts)},
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
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


def store_embedding(
    db: Database,
    memory_id: str,
    vector: list[float],
    *,
    model_id: str,
    configured_dim: int | None,
) -> None:
    """L2 归一化 + float32 小端入库，并在同一写事务内维护 model_id/dim 指纹。"""
    dim = len(vector)
    if dim == 0:
        raise RuntimeError("embedding 向量为空")
    packed = pack_vector(vector)
    with db.write_txn() as conn:
        _write_fingerprint(conn, model_id, dim, configured_dim)
        conn.execute(
            "INSERT OR REPLACE INTO embeddings(memory_id, vector) VALUES (?, ?)",
            (memory_id, packed),
        )


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
        _assert_matches(stored_model, stored_dim, provider.model_id, configured_dim, configured_dim)
    return stored_model, stored_dim


def _first_vector_dim(conn: sqlite3.Connection) -> int | None:
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
    """内存矩阵（只回 id + 距离），按 (条数, 最大 rowid) 失效重建。

    1024 维 × 1 万条 ≈ 40MB，暴力点积 ms 级（§3.2）。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._key: tuple[int, int] | None = None
        self._ids: tuple[str, ...] = ()
        self._matrix: np.ndarray | None = None

    def invalidate(self) -> None:
        with self._lock:
            self._key = None
            self._ids = ()
            self._matrix = None

    def load(self, db: Database) -> tuple[tuple[str, ...], np.ndarray]:
        conn = db.read_conn
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(rowid), 0) FROM embeddings"
        ).fetchone()
        key = (int(row[0]), int(row[1]))
        with self._lock:
            if key == self._key and self._matrix is not None:
                return self._ids, self._matrix
        rows = conn.execute(
            "SELECT memory_id, vector FROM embeddings ORDER BY memory_id"
        ).fetchall()
        if not rows:
            ids: tuple[str, ...] = ()
            matrix = np.zeros((0, 0), dtype=np.float32)
        else:
            dim = len(rows[0][1]) // 4
            vectors = [unpack_vector(r[1], dim) for r in rows]
            ids = tuple(r[0] for r in rows)
            matrix = np.vstack(vectors).astype(np.float32, copy=False)
        with self._lock:
            self._key = key
            self._ids = ids
            self._matrix = matrix
        return ids, matrix
