"""L3 仲裁器（QuotaGateArbiter）行为契约。

规则：
- 资源健康 → 完全透传（与 PassThroughArbiter 等价）
- circuit open → 换 fallback provider，深度/动作不变
- quota 忙 → 轻意图升级 FRONT/QUEUE_FRONT，重意图升级 HERMES/ENQUEUE_DEEP_TASK
- DCRouter._decide_business 必须真正采用仲裁结果（占位时代 arbitrate 返回值被丢弃）
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dc_router_core.entrypoint import DCRouter, MessageEnvelope
from dc_router_core.provider_map import get_provider_route
from dc_router_core.taxonomy import RouteAction, RouteDepth, RouterIntent
from harness.quota_gate import QuotaGate, QuotaRequest
from harness.route_arbiter import QuotaGateArbiter

ANTIGRAVITY_KEY = "antigravity_cli_flash"


def _circuit(allowed: bool, reason: str = "timeout streak"):
    def _checker():
        return allowed, reason, {"remaining_seconds": 120}

    return _checker


def _envelope(text: str = "随便聊聊") -> MessageEnvelope:
    return MessageEnvelope(text=text)


async def _busy_gate(tmp_path: Path) -> QuotaGate:
    gate = QuotaGate(tmp_path / "quota.db")
    decision = await gate.admit(
        QuotaRequest(
            primary_resource_key=ANTIGRAVITY_KEY,
            resource_keys=(ANTIGRAVITY_KEY,),
        )
    )
    assert decision.mode.value == "run_now"
    return gate


@pytest.mark.asyncio
async def test_healthy_resources_pass_through(tmp_path: Path):
    gate = QuotaGate(tmp_path / "quota.db")
    arbiter = QuotaGateArbiter(quota_gate=gate, circuit_checker=_circuit(True))
    route = get_provider_route(RouterIntent.CASUAL)

    result = await arbiter.arbitrate(route, _envelope(), {})

    assert result.route is route
    assert result.depth == route.depth
    assert result.action == route.action
    assert result.reason == "pass-through"


@pytest.mark.asyncio
async def test_circuit_open_swaps_provider_keeps_depth(tmp_path: Path):
    arbiter = QuotaGateArbiter(circuit_checker=_circuit(False, "cli timeout"))
    route = get_provider_route(RouterIntent.CASUAL)
    assert route.provider_id.startswith("cli/antigravity/")

    result = await arbiter.arbitrate(route, _envelope(), {"k": "v"})

    assert result.route.provider_id == "aihubmix/gemini-3.5-flash"
    assert result.depth == route.depth
    assert result.action == route.action
    assert result.metadata["arbiter_circuit_fallback"] == "true"
    assert result.metadata["arbiter_circuit_reason"] == "cli timeout"
    assert result.metadata["k"] == "v"


@pytest.mark.asyncio
async def test_circuit_check_skips_non_cli_providers(tmp_path: Path):
    arbiter = QuotaGateArbiter(circuit_checker=_circuit(False))
    route = get_provider_route(RouterIntent.CREATIVE)  # aihubmix provider

    result = await arbiter.arbitrate(route, _envelope(), {})

    assert result.route is route
    assert result.reason == "pass-through"


@pytest.mark.asyncio
async def test_quota_busy_queues_light_intent_at_front(tmp_path: Path):
    gate = await _busy_gate(tmp_path)
    arbiter = QuotaGateArbiter(quota_gate=gate)
    route = get_provider_route(RouterIntent.CASUAL)
    busy_route = type(route)(
        intent=route.intent,
        provider_id=route.provider_id,
        depth=route.depth,
        action=route.action,
        target_model=route.target_model,
        resource_keys=(ANTIGRAVITY_KEY,),
    )

    result = await arbiter.arbitrate(busy_route, _envelope(), {})

    assert result.depth == RouteDepth.FRONT
    assert result.action == RouteAction.QUEUE_FRONT
    assert result.metadata["arbiter_quota_busy"] == "true"


@pytest.mark.asyncio
async def test_quota_busy_escalates_heavy_intent_to_hermes(tmp_path: Path):
    gate = await _busy_gate(tmp_path)
    arbiter = QuotaGateArbiter(quota_gate=gate)
    route = get_provider_route(RouterIntent.DEEP_INSIGHT)
    busy_route = type(route)(
        intent=route.intent,
        provider_id=route.provider_id,
        depth=route.depth,
        action=route.action,
        target_model=route.target_model,
        resource_keys=(ANTIGRAVITY_KEY,),
    )

    result = await arbiter.arbitrate(busy_route, _envelope(), {})

    assert result.depth == RouteDepth.HERMES
    assert result.action == RouteAction.ENQUEUE_DEEP_TASK


@pytest.mark.asyncio
async def test_quota_available_after_completion_passes_through(tmp_path: Path):
    gate = QuotaGate(tmp_path / "quota.db")
    decision = await gate.admit(
        QuotaRequest(
            primary_resource_key=ANTIGRAVITY_KEY,
            resource_keys=(ANTIGRAVITY_KEY,),
        )
    )
    await gate.complete(decision.job.job_id, cooldown_seconds=0)
    arbiter = QuotaGateArbiter(quota_gate=gate)
    route = get_provider_route(RouterIntent.CASUAL)
    keyed_route = type(route)(
        intent=route.intent,
        provider_id=route.provider_id,
        depth=route.depth,
        action=route.action,
        target_model=route.target_model,
        resource_keys=(ANTIGRAVITY_KEY,),
    )

    result = await arbiter.arbitrate(keyed_route, _envelope(), {})

    assert result.reason == "pass-through"


@pytest.mark.asyncio
async def test_dc_router_applies_arbitration_result(tmp_path: Path):
    """决策必须反映仲裁结果——锁死占位时代'调用但丢弃返回值'的旧行为。"""
    gate = await _busy_gate(tmp_path)
    arbiter = QuotaGateArbiter(
        quota_gate=gate,
        circuit_checker=_circuit(False, "cli timeout"),
    )
    router = DCRouter(arbiter=arbiter)

    decision = await router.decide(_envelope("帮我看看今天天气怎么样"))

    # CASUAL/FALLBACK 路由的 provider 是 cli/antigravity/*，circuit open 必须换走
    assert decision.provider_id == "aihubmix/gemini-3.5-flash"
    assert decision.metadata["arbiter_circuit_fallback"] == "true"


@pytest.mark.asyncio
async def test_dc_router_pass_through_keeps_legacy_decision(tmp_path: Path):
    router = DCRouter()

    decision = await router.decide(_envelope("帮我看看今天天气怎么样"))

    assert decision.provider_id.startswith("cli/antigravity/")
    assert "arbiter_circuit_fallback" not in decision.metadata
