"""embedding 传输层：代理环境解析（联调实测 httpx.InvalidURL 崩溃的回归测试）。

2026-11-12 真机日志：``httpx.InvalidURL: Invalid port: ':1]'``，原因是本机
``NO_PROXY`` 含 ``[::1]``，而 ``httpx.Client(trust_env=True)`` 在构造时解析代理
环境就崩了——embedding 请求根本没发出去。默认 ``trust_env=False`` 直连。
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.config import load_config
from app.embeddings import (
    EmbeddingRequestError,
    OpenAICompatProvider,
    build_provider,
)


def _provider(*, trust_env: bool = False) -> OpenAICompatProvider:
    return OpenAICompatProvider(
        base_url="http://127.0.0.1:1/v1",
        api_key="sk-test",
        model_id="test-embedding",
        timeout_seconds=0.1,
        trust_env=trust_env,
    )


def test_malformed_no_proxy_does_not_break_default_client(monkeypatch):
    """畸形 NO_PROXY 下，默认直连的 client 构造不得抛异常。"""
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1,[::1]")
    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1,[::1]")
    provider = _provider()
    assert provider.trust_env is False
    client = provider._make_client()
    client.close()


def test_malformed_no_proxy_with_trust_env_is_classified_error(monkeypatch):
    """显式 trust_env=True 时构造失败 → 分类错误（不是裸 httpx.InvalidURL）。"""
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1,[::1]")
    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1,[::1]")
    provider = _provider(trust_env=True)
    with pytest.raises(EmbeddingRequestError) as excinfo:
        provider._make_client()
    message = str(excinfo.value)
    assert "embedding HTTP client 初始化失败" in message
    assert "EMBEDDING_TRUST_ENV=true" in message


def test_client_construction_failure_is_classified(monkeypatch):
    """任意 client 构造异常都转换为 EmbeddingRequestError，绝不裸 traceback。"""
    import httpx

    class BoomClient:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("proxy env exploded")

    monkeypatch.setattr(httpx, "Client", BoomClient)
    with pytest.raises(EmbeddingRequestError) as excinfo:
        _provider()._make_client()
    assert "proxy env exploded" in str(excinfo.value)


def test_config_embedding_trust_env_defaults_false_and_can_enable(monkeypatch):
    monkeypatch.delenv("EMBEDDING_TRUST_ENV", raising=False)
    monkeypatch.delenv("DSH_EMBEDDING_TRUST_ENV", raising=False)
    assert load_config().embedding_trust_env is False

    monkeypatch.setenv("EMBEDDING_TRUST_ENV", "1")
    assert load_config().embedding_trust_env is True


def test_build_provider_passes_trust_env_from_config():
    cfg = replace(
        load_config(),
        embedding_backend="openai-compat",
        embedding_base_url="http://127.0.0.1:1/v1",
        embedding_model="test-embedding",
        embedding_trust_env=True,
    )
    provider = build_provider(cfg)
    assert isinstance(provider, OpenAICompatProvider)
    assert provider.trust_env is True
