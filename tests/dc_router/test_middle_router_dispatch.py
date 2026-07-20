"""Integration seam tests for Agent-first dispatch."""

from __future__ import annotations

import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from data.plugins.dc_router.config import DCRouterConfig
from data.plugins.dc_router.preprocessing.card_action import CardActionResult
from data.plugins.dc_router.routing.apply_decision import apply_provider_pin

dispatch_module = importlib.import_module("data.plugins.dc_router.dispatch")


def _event() -> MagicMock:
    event = MagicMock()
    event.message_str = "帮我策划一个新品发布活动"
    event.unified_msg_origin = "巅池-Agent小助手:FriendMessage:ou_user"
    event.message_obj = SimpleNamespace(message=[], message_str=event.message_str)
    event.get_platform_id.return_value = "巅池-Agent小助手"
    event.get_sender_id.return_value = "ou_user"
    extras: dict[str, object] = {}
    event.get_extra.side_effect = lambda key, default=None: extras.get(key, default)
    event.set_extra.side_effect = lambda key, value: extras.__setitem__(key, value)
    event._extras = extras
    return event


@pytest.mark.asyncio
async def test_provider_pin_selects_luna_for_current_request() -> None:
    event = _event()
    context = MagicMock()
    context.get_provider_by_id.return_value = object()
    context.provider_manager.set_provider = AsyncMock()

    pinned = await apply_provider_pin(
        context,
        event,
        target_provider_id="codex/gpt-5.6-luna",
        source="main_agent",
        intent="conversation_or_agent_decision",
    )

    assert pinned is True
    assert event._extras["selected_provider"] == "codex/gpt-5.6-luna"
    assert event._extras["dc_router_provider"] == "codex/gpt-5.6-luna"


@pytest.mark.asyncio
async def test_middle_mode_pins_luna_and_bypasses_legacy_classifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = _event()
    context = MagicMock()
    cfg = DCRouterConfig(
        enabled=True,
        dry_run=False,
        architecture_mode="middle",
        main_agent_provider_id="codex/gpt-5.6-luna",
    )
    pin = AsyncMock(return_value=True)
    legacy_router = AsyncMock()
    fallback = AsyncMock()

    monkeypatch.setattr(
        dispatch_module,
        "try_handle_card_action",
        AsyncMock(return_value=CardActionResult(handled=False)),
    )
    monkeypatch.setattr(
        dispatch_module, "try_handle_assistant_workbench", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(
        dispatch_module, "_build_memory_query", AsyncMock(return_value="q")
    )
    monkeypatch.setattr(
        dispatch_module, "_memory_injection", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        dispatch_module, "try_inject_assistant_tone", MagicMock(return_value=True)
    )
    monkeypatch.setattr(dispatch_module, "apply_provider_pin", pin)
    monkeypatch.setattr(dispatch_module, "_run_dc_router", legacy_router)
    monkeypatch.setattr(dispatch_module, "_v1_fallback", fallback)
    monkeypatch.setattr(dispatch_module, "record_router_pet_event", MagicMock())

    result = await dispatch_module.dispatch(context, event, cfg)

    assert result.handled is False
    assert result.source == "main_agent"
    assert result.decision_provider == "codex/gpt-5.6-luna"
    assert event._extras["dc_router_architecture"] == "middle"
    assert event._extras["dc_middle_router_enforce_handoffs"] is True
    pin.assert_awaited_once()
    assert pin.await_args.kwargs["target_provider_id"] == "codex/gpt-5.6-luna"
    legacy_router.assert_not_awaited()
    fallback.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("architecture_mode", ["middle", "legacy_front"])
async def test_structured_media_decision_executes_without_reclassification(
    monkeypatch: pytest.MonkeyPatch,
    architecture_mode: str,
) -> None:
    event = _event()
    event._extras["dc_middle_router_executor"] = "media_route"
    event._extras["dc_middle_router_capability"] = "execute.image"
    event._extras["dc_middle_router_goal"] = "第二张北欧冰雪足球海报变体"
    event._extras["dc_middle_router_parameters"] = {
        "aspect_ratio": "3:4",
        "image_count": "2",
    }
    context = MagicMock()
    cfg = DCRouterConfig(
        enabled=True,
        dry_run=False,
        architecture_mode=architecture_mode,
    )
    media = AsyncMock(return_value=True)
    legacy_router = AsyncMock()

    monkeypatch.setattr(
        dispatch_module,
        "try_handle_card_action",
        AsyncMock(return_value=CardActionResult(handled=False)),
    )
    monkeypatch.setattr(
        dispatch_module, "try_handle_assistant_workbench", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(dispatch_module, "try_handle_media_route", media)
    monkeypatch.setattr(dispatch_module, "_run_dc_router", legacy_router)
    monkeypatch.setattr(dispatch_module, "record_router_pet_event", MagicMock())

    result = await dispatch_module.dispatch(context, event, cfg)

    assert result.handled is True
    assert result.source == "middle_router:media_route"
    assert result.decision_intent == "execute.image"
    media.assert_awaited_once_with(
        context,
        event,
        "第二张北欧冰雪足球海报变体",
        capability_id="execute.image",
        parameters={"aspect_ratio": "3:4", "image_count": "2"},
    )
    legacy_router.assert_not_awaited()
