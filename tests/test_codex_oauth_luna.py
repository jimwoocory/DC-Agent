"""Runtime catalog tests for the Luna main Agent provider."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from astrbot.core.provider.sources import codex_oauth_source
from astrbot.core.provider.sources.codex_oauth_source import (
    SUPPORTED_MODELS,
    VALID_EFFORTS,
    ProviderCodexOAuth,
)


@pytest.mark.asyncio
async def test_codex_oauth_catalog_exposes_gpt_5_6_luna() -> None:
    provider = ProviderCodexOAuth.__new__(ProviderCodexOAuth)

    assert "gpt-5.6-luna" in await provider.get_models()
    assert "gpt-5.6-luna" in SUPPORTED_MODELS
    assert "max" in VALID_EFFORTS


def test_codex_oauth_provider_uses_configured_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ensure the NAS provider sends Codex traffic through its proxy.

    Args:
        monkeypatch: Pytest fixture used to isolate network client construction.
    """
    captured: dict[str, dict] = {}

    class FakeAsyncClient:
        """Capture HTTP client construction without opening a connection."""

        def __init__(self, **kwargs: object) -> None:
            captured["http_client"] = kwargs

    class FakeAsyncOpenAI:
        """Capture SDK client construction without validating the fake client."""

        def __init__(self, **kwargs: object) -> None:
            captured["openai"] = kwargs

    monkeypatch.setattr(codex_oauth_source, "AsyncOpenAI", FakeAsyncOpenAI)
    monkeypatch.setattr(codex_oauth_source.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(
        codex_oauth_source,
        "_read_codex_access_token",
        lambda: "test-token",
    )

    provider = ProviderCodexOAuth(
        {
            "id": "codex/gpt-5.6-luna",
            "type": "codex_oauth_chat",
            "model_config": {"model": "gpt-5.6-luna"},
            "proxy": "http://aihubmix-aws-tunnel:7898",
            "timeout": 30,
        },
        {},
    )

    assert provider.proxy == "http://aihubmix-aws-tunnel:7898"
    assert captured["http_client"]["proxy"] == provider.proxy
    assert captured["openai"]["http_client"].__class__ is FakeAsyncClient


def test_architecture_contract_requires_luna_and_specialist_subagents() -> None:
    root = Path(__file__).resolve().parents[1]
    contract = json.loads(
        (root / "harness" / "contracts" / "middle_router_architecture.json").read_text(
            encoding="utf-8"
        )
    )

    rollout = contract["rollout"]
    architecture = contract["architecture"]

    assert rollout["main_agent_provider"] == "codex/gpt-5.6-luna"
    assert rollout["main_agent_model"] == "gpt-5.6-luna"
    assert rollout["main_agent_proxy_config_key"] == "proxy"
    assert rollout["required_main_agent_modality"] == "tool_use"
    assert set(architecture["subagents"]) == {
        "research_agent",
        "office_agent",
        "analysis_agent",
    }


def test_nas_compose_exposes_global_http_tunnel_with_local_bypass() -> None:
    """Keep external HTTP traffic on the NAS tunnel without proxying IM sockets."""
    root = Path(__file__).resolve().parents[1]
    compose = (root / "deploy" / "nas-unified" / "compose.yml").read_text(
        encoding="utf-8"
    )

    assert 'HTTP_PROXY: "http://aihubmix-aws-tunnel:7898"' in compose
    assert 'HTTPS_PROXY: "http://aihubmix-aws-tunnel:7898"' in compose
    assert 'http_proxy: "http://aihubmix-aws-tunnel:7898"' in compose
    assert 'https_proxy: "http://aihubmix-aws-tunnel:7898"' in compose
    assert ".feishu.cn" in compose
    assert ".dingtalk.com" in compose
    assert "192.168.1.35" in compose
