"""Regression tests for confirmed media execution from the Luna main Agent."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from data.plugins.dc_router import main as router_main
from data.plugins.dc_router.dispatch import DispatchResult


def _plugin_event(*, auto_submit: bool) -> MagicMock:
    """Build one event for the plugin-level workbench routing seam."""
    event = MagicMock()
    event.message_str = '__card_action__:{"value":{"source":"assistant_workbench"}}'
    extras: dict[str, object] = {
        "assistant_workbench_auto_submit": auto_submit,
    }
    event.get_extra.side_effect = lambda key, default=None: extras.get(key, default)
    event.set_extra.side_effect = lambda key, value: extras.__setitem__(key, value)
    event._extras = extras
    return event


@pytest.mark.asyncio
async def test_internal_h5_submission_uses_early_router_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Route a trusted H5 save before unrelated card-action handlers can stop it."""
    event = _plugin_event(auto_submit=True)
    plugin = object.__new__(router_main.DCRouterPlugin)
    plugin.context = MagicMock()
    cfg = SimpleNamespace(uses_middle_router=True)
    routed = AsyncMock(
        return_value=DispatchResult(
            handled=True,
            source="middle_router:media_route",
            decision_intent="execute.image",
        )
    )
    monkeypatch.setattr(router_main, "load_config", lambda: cfg)
    monkeypatch.setattr(router_main, "dispatch", routed)

    await plugin.route_internal_workbench_submission(event)

    routed.assert_awaited_once_with(plugin.context, event, cfg)
    assert "dc_internal_workbench_pre_routed" not in event._extras


@pytest.mark.asyncio
async def test_internal_h5_nonterminal_route_is_not_dispatched_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep copy-like H5 submissions in the LLM pipeline without rerouting them."""
    event = _plugin_event(auto_submit=True)
    plugin = object.__new__(router_main.DCRouterPlugin)
    plugin.context = MagicMock()
    cfg = SimpleNamespace(uses_middle_router=True)
    routed = AsyncMock(return_value=DispatchResult(handled=False, source="main_agent"))
    monkeypatch.setattr(router_main, "load_config", lambda: cfg)
    monkeypatch.setattr(router_main, "dispatch", routed)

    await plugin.route_internal_workbench_submission(event)
    await plugin.route(event)

    routed.assert_awaited_once_with(plugin.context, event, cfg)
    assert event._extras["dc_internal_workbench_pre_routed"] is True


@pytest.mark.asyncio
async def test_ordinary_card_event_skips_internal_h5_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Leave external card callbacks on the channel-governed plugin path."""
    event = _plugin_event(auto_submit=False)
    plugin = object.__new__(router_main.DCRouterPlugin)
    plugin.context = MagicMock()
    routed = AsyncMock()
    monkeypatch.setattr(router_main, "dispatch", routed)

    await plugin.route_internal_workbench_submission(event)

    routed.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirmed_image_decision_executes_full_goal_without_opening_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Execute a confirmed image prompt through media_route in the same tool call."""
    event = MagicMock()
    event.get_platform_id.return_value = "巅池-Agent小助手"
    event.get_sender_id.return_value = "ou_user"
    extras: dict[str, object] = {}
    event.get_extra.side_effect = lambda key, default=None: extras.get(key, default)
    event.set_extra.side_effect = lambda key, value: extras.__setitem__(key, value)
    plugin = object.__new__(router_main.DCRouterPlugin)
    plugin.context = MagicMock()
    goal = "原创挪威红、蓝、白配色的北欧冰雪足球怪兽海报"
    media = AsyncMock(return_value=True)
    workspace = AsyncMock()

    monkeypatch.setattr(
        router_main,
        "load_config",
        lambda: SimpleNamespace(uses_middle_router=True),
    )
    monkeypatch.setattr(router_main, "is_business_platform", lambda _value: True)
    monkeypatch.setattr(router_main, "try_handle_media_route", media)
    monkeypatch.setattr(
        "data.plugins.dc_router.preprocessing.assistant_workbench._send_task_workspace",
        workspace,
    )

    result = json.loads(
        await plugin.route_agent_decision(
            event,
            capability_id="execute.image",
            goal=goal,
            confidence=0.98,
            action_force="execute",
        )
    )

    assert result["allowed"] is True
    assert result["target"] == "harness_executor"
    assert result["execution_started"] is True
    media.assert_awaited_once_with(
        plugin.context,
        event,
        goal,
        capability_id="execute.image",
        parameters={},
    )
    workspace.assert_not_awaited()


def test_main_agent_contract_distinguishes_confirmed_execution_from_navigation() -> (
    None
):
    """Keep menu-like or incomplete requests in H5 while executing confirmed prompts."""
    prompt = router_main._MAIN_AGENT_ROUTING_PROMPT

    assert "execute.image" in prompt
    assert "完整" in prompt
    assert "确认" in prompt
    assert "workspace.image" in prompt


def test_main_agent_contract_requires_grounded_company_knowledge_answers() -> None:
    """Require Luna to retrieve and qualify evidence for internal facts."""
    prompt = router_main._MAIN_AGENT_ROUTING_PROMPT

    assert "公司、客户、项目、人员" in prompt
    assert "已批准" in prompt
    assert "已确认事实" in prompt
    assert "资料推断" in prompt
    assert "暂未确认" in prompt
    assert "来源路径" in prompt
    assert "版本冲突" in prompt
