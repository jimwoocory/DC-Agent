from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

DC_ENGINES_PATH = Path(__file__).resolve().parents[1] / "dc_engines"
if str(DC_ENGINES_PATH) not in sys.path:
    sys.path.insert(0, str(DC_ENGINES_PATH))

from dc_engines.feishu_card_streamer import build_quiz_result_card  # noqa: E402


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
    event.send = AsyncMock()
    event.plain_result = MagicMock(side_effect=lambda text: SimpleNamespace(text=text))
    event.stop_event = MagicMock()
    return event


class _EmployeeStore:
    def __init__(self, employee: Any) -> None:
        self.employee = employee
        self.updated_preferences: dict[str, Any] | None = None
        self.created_open_ids: list[str] = []

    async def get_employee(self, open_id: str) -> Any:
        return self.employee

    async def get_or_create(
        self, open_id: str, *, platform_id: str = ""
    ) -> tuple[Any, bool]:
        self.created_open_ids.append(open_id)
        if self.employee is None:
            self.employee = SimpleNamespace(display_name="", preferences={})
            return self.employee, True
        return self.employee, False

    async def update_profile(self, open_id: str, **updates: Any) -> None:
        if "preferences" in updates:
            self.employee.preferences = updates["preferences"]
            self.updated_preferences = updates["preferences"]


class _SlowEmployeeStore:
    async def get_employee(self, open_id: str) -> Any:
        await asyncio.sleep(1)
        return None

    async def get_or_create(
        self, open_id: str, *, platform_id: str = ""
    ) -> tuple[Any, bool]:
        raise AssertionError("slow first-message path should be cancelled")


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


@pytest.mark.asyncio
async def test_auto_invite_failure_with_placeholder_link_keeps_onboarding_done() -> (
    None
):
    plugin = _make_plugin(
        {
            "enabled": True,
            "maintenance_mode": False,
            "auto_invite_to_chat": True,
            "internal_test_chat_id": "oc_real_chat",
        }
    )
    plugin._chat_creator = SimpleNamespace(
        invite_members=AsyncMock(return_value=(0, [], "feishu_api_error"))
    )

    next_stage, note, err = await plugin._invite_after_pass("ou_test_user")

    assert next_stage == "done"
    assert err == "feishu_api_error"
    assert "下方链接" not in note
    assert "正常使用" in note


@pytest.mark.asyncio
async def test_quiz_result_with_auto_invite_failure_still_marks_done() -> None:
    employee = SimpleNamespace(
        display_name="测试同学",
        preferences={
            "_onboarding": {
                "stage": "quiz_active",
                "quiz_correct_count": 6,
                "quiz_total": 6,
            }
        },
    )
    store = _EmployeeStore(employee)
    plugin = _make_plugin(
        {
            "enabled": True,
            "maintenance_mode": False,
            "auto_invite_to_chat": True,
            "internal_test_chat_id": "oc_real_chat",
        },
        store=store,
    )
    plugin._chat_creator = SimpleNamespace(
        invite_members=AsyncMock(return_value=(0, [], "feishu_api_error"))
    )
    plugin._send_card = AsyncMock(return_value="msg_result")

    await plugin._show_quiz_result(_make_event("看结果"))

    state = employee.preferences["_onboarding"]
    assert state["stage"] == "done"
    assert state["invite_error"] == "feishu_api_error"
    assert await plugin._is_onboarded("ou_test_user") is True
    card = plugin._send_card.call_args.args[1]
    card_text = json.dumps(card, ensure_ascii=False)
    assert "https://o0ain5w98jh.feishu.cn/q/" not in card_text
    assert "下方链接" not in card_text


@pytest.mark.asyncio
async def test_valid_invite_link_result_card_failure_still_marks_done() -> None:
    employee = SimpleNamespace(
        display_name="测试同学",
        preferences={
            "_onboarding": {
                "stage": "quiz_active",
                "quiz_correct_count": 6,
                "quiz_total": 6,
            }
        },
    )
    store = _EmployeeStore(employee)
    plugin = _make_plugin(
        {
            "enabled": True,
            "maintenance_mode": False,
            "invite_link": "https://example.feishu.cn/share/base/form/real",
        },
        store=store,
    )
    plugin._send_card = AsyncMock(return_value=None)

    await plugin._show_quiz_result(_make_event("看结果"))

    state = employee.preferences["_onboarding"]
    assert state["stage"] == "done"
    assert state["invite_error"] is None
    assert "invite_message_id" not in state
    assert await plugin._is_onboarded("ou_test_user") is True


@pytest.mark.asyncio
async def test_valid_invite_link_is_optional_after_done() -> None:
    employee = SimpleNamespace(
        display_name="测试同学",
        preferences={
            "_onboarding": {
                "stage": "quiz_active",
                "quiz_correct_count": 6,
                "quiz_total": 6,
            }
        },
    )
    store = _EmployeeStore(employee)
    plugin = _make_plugin(
        {
            "enabled": True,
            "maintenance_mode": False,
            "invite_link": "https://example.feishu.cn/share/base/form/real",
        },
        store=store,
    )
    plugin._send_card = AsyncMock(return_value="msg_invite")

    await plugin._show_quiz_result(_make_event("看结果"))

    state = employee.preferences["_onboarding"]
    assert state["stage"] == "done"
    assert state["invite_link"] == "https://example.feishu.cn/share/base/form/real"
    assert state["invite_message_id"] == "msg_invite"
    assert state["invite_delivered_at"]
    assert await plugin._is_onboarded("ou_test_user") is True


@pytest.mark.asyncio
async def test_auto_invite_success_marks_joined() -> None:
    employee = SimpleNamespace(
        display_name="测试同学",
        preferences={
            "_onboarding": {
                "stage": "quiz_active",
                "quiz_correct_count": 6,
                "quiz_total": 6,
            }
        },
    )
    store = _EmployeeStore(employee)
    plugin = _make_plugin(
        {
            "enabled": True,
            "maintenance_mode": False,
            "auto_invite_to_chat": True,
            "internal_test_chat_id": "oc_real_chat",
        },
        store=store,
    )
    plugin._chat_creator = SimpleNamespace(
        invite_members=AsyncMock(return_value=(1, [], None))
    )
    plugin._send_card = AsyncMock(return_value=None)

    await plugin._show_quiz_result(_make_event("看结果"))

    state = employee.preferences["_onboarding"]
    assert state["stage"] == "joined"
    assert await plugin._is_onboarded("ou_test_user") is True


@pytest.mark.asyncio
async def test_auto_invite_invalid_member_still_marks_done() -> None:
    plugin = _make_plugin(
        {
            "enabled": True,
            "maintenance_mode": False,
            "auto_invite_to_chat": True,
            "internal_test_chat_id": "oc_real_chat",
            "invite_link": "https://example.feishu.cn/share/base/form/real",
        }
    )
    plugin._chat_creator = SimpleNamespace(
        invite_members=AsyncMock(return_value=(0, ["ou_test_user"], None))
    )

    next_stage, note, err = await plugin._invite_after_pass("ou_test_user")

    assert next_stage == "done"
    assert err == "invalid_or_already_member"
    assert "下方链接" not in note
    assert "正常使用" in note


@pytest.mark.asyncio
async def test_open_mode_unfinished_onboarding_allows_business_first_message() -> None:
    store = _EmployeeStore(None)
    plugin = _make_plugin(
        {
            "enabled": True,
            "maintenance_mode": False,
            "block_business_until_complete": False,
        },
        store=store,
    )
    plugin._start_onboarding = AsyncMock()
    event = _make_event("帮我整理明天活动执行物料清单")

    await plugin.on_lark_private(event)

    assert store.created_open_ids == ["ou_test_user"]
    plugin._start_onboarding.assert_not_called()
    event.stop_event.assert_not_called()
    event.send.assert_not_called()


@pytest.mark.asyncio
async def test_onboarding_timeout_fails_open_for_business_first_message() -> None:
    plugin = _make_plugin(
        {
            "enabled": True,
            "maintenance_mode": False,
            "block_business_until_complete": False,
            "side_effect_timeout_seconds": 0.01,
        },
        store=_SlowEmployeeStore(),
    )
    event = _make_event("帮我整理明天活动执行物料清单")
    start = time.monotonic()

    await plugin.on_lark_private(event)

    assert time.monotonic() - start < 0.5
    event.stop_event.assert_not_called()
    event.send.assert_not_called()
    event.set_result.assert_not_called()


@pytest.mark.asyncio
async def test_legacy_invite_pending_allows_private_messages() -> None:
    employee = SimpleNamespace(
        display_name="测试同学",
        preferences={"_onboarding": {"stage": "invite_pending"}},
    )
    store = _EmployeeStore(employee)
    plugin = _make_plugin(
        {"enabled": True, "maintenance_mode": False},
        store=store,
    )
    plugin._start_onboarding = AsyncMock()
    event = _make_event("我可以开始用了吗")

    await plugin.on_lark_private(event)

    plugin._start_onboarding.assert_not_called()
    event.stop_event.assert_not_called()
    event.send.assert_not_called()
