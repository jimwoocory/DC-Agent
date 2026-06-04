from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from dc_engines.feishu_card_streamer import build_quiz_result_card


def _load_onboarding_module():
    module_path = (
        Path(__file__).resolve().parents[1]
        / "data"
        / "plugins"
        / "employee_onboarding"
        / "main.py"
    )
    spec = importlib.util.spec_from_file_location(
        "dc_employee_onboarding_test",
        module_path,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_plugin(config: dict[str, Any] | None = None, *, store: Any = None):
    module = _load_onboarding_module()
    context = SimpleNamespace(employee_store=store, platform_manager=None)
    return module.EmployeeOnboardingPlugin(context, config or {})


def _make_event(
    text: str,
    *,
    is_admin: bool = False,
    trusted_card_action: bool = False,
):
    event = MagicMock()
    event.message_str = text
    event.get_platform_id = MagicMock(return_value="巅池-Agent小助手")
    event.get_sender_id = MagicMock(return_value="ou_test_user")
    event.get_group_id = MagicMock(return_value="")
    event.is_admin = MagicMock(return_value=is_admin)
    event.message_obj = SimpleNamespace(
        raw_message=None,
        is_card_action=trusted_card_action,
    )
    event.set_result = MagicMock()
    event.stop_event = MagicMock()
    return event


@pytest.mark.asyncio
async def test_onboarding_disabled_by_default_blocks_private() -> None:
    plugin = _make_plugin()
    plugin._start_onboarding = AsyncMock()

    event = _make_event("在吗")

    await plugin.on_lark_private(event)

    assert plugin.enabled is False
    assert plugin.maintenance_mode is True
    plugin._start_onboarding.assert_not_called()
    event.stop_event.assert_not_called()


@pytest.mark.asyncio
async def test_card_action_requires_trusted_source() -> None:
    plugin = _make_plugin({"enabled": True, "maintenance_mode": False})
    plugin._on_submit_quiz = AsyncMock()
    payload = {
        "value": {
            "action": "submit_quiz",
            "q_num": 5,
            "choice": "B",
        }
    }
    event = _make_event("__card_action__:" + json.dumps(payload, ensure_ascii=False))

    await plugin._handle_card_action(event, event.message_str)

    plugin._on_submit_quiz.assert_not_called()
    event.set_result.assert_not_called()


@pytest.mark.asyncio
async def test_admin_command_requires_permission() -> None:
    plugin = _make_plugin(
        {"enabled": True, "maintenance_mode": False},
        store=object(),
    )
    plugin._run_outreach_scan = AsyncMock()
    event = _make_event("/onboarding scan", is_admin=False)

    await plugin.onboarding_command(event)

    plugin._run_outreach_scan.assert_not_called()
    event.set_result.assert_called_once()
    result_text = event.set_result.call_args.args[0].get_plain_text()
    assert "权限不足" in result_text


@pytest.mark.asyncio
async def test_force_disable_env_overrides_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EMPLOYEE_ONBOARDING_FORCE_DISABLE", "1")

    plugin = _make_plugin({"enabled": True, "maintenance_mode": False})
    report = await plugin._run_outreach_scan(force=True)

    assert plugin.enabled is False
    assert plugin.maintenance_mode is True
    assert report["disabled"] == 1


def test_quiz_result_placeholder_invite_link_is_not_rendered() -> None:
    card = build_quiz_result_card(
        display_name="测试同学",
        correct_count=5,
        total=5,
        invite_link="https://o0ain5w98jh.feishu.cn/q/...（待补真实链接）",
    )

    card_text = json.dumps(card, ensure_ascii=False)
    assert "群链接稍后开放" in card_text
    assert "或直接复制群链接" not in card_text
    assert "https://o0ain5w98jh.feishu.cn/q/" not in card_text
