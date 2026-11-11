"""测试用确定性 embedding 假实现（不联网）。"""

from __future__ import annotations

import hashlib

import numpy as np


class FixedEmbeddingProvider:
    """按文本查表返回固定向量（用于验证点积排序）。"""

    def __init__(self, vectors: dict[str, list[float]], model_id: str = "fixed-test") -> None:
        self.vectors = {key: [float(x) for x in value] for key, value in vectors.items()}
        self.model_id = model_id

    def embed(self, texts: list[str]) -> list[list[float]]:
        missing = [text for text in texts if text not in self.vectors]
        if missing:
            raise KeyError(f"FixedEmbeddingProvider 没有准备这些文本的向量：{missing}")
        return [list(self.vectors[text]) for text in texts]


class HashingEmbeddingProvider:
    """按文本哈希生成确定性单位向量（同文本永远同向量）。"""

    def __init__(self, dim: int = 32, model_id: str = "hash-test") -> None:
        self.dim = dim
        self.model_id = model_id

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            digest = hashlib.blake2b(text.encode("utf-8"), digest_size=16).digest()
            rng = np.random.default_rng(int.from_bytes(digest, "big"))
            vector = rng.standard_normal(self.dim)
            norm = float(np.linalg.norm(vector))
            if norm == 0:  # pragma: no cover - 概率上不可能
                vector = np.ones(self.dim, dtype=float)
                norm = float(np.linalg.norm(vector))
            vectors.append((vector / norm).tolist())
        return vectors


class FailingEmbeddingProvider:
    """模拟 embedding API 超时/报错：向量路必须降级，不得整查询 500。"""

    model_id = "failing-test"

    def __init__(self) -> None:
        self.calls = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        raise TimeoutError("embedding API timeout（测试模拟）")
