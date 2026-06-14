from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

DC_ENGINES_PATH = Path(__file__).resolve().parents[1] / "dc_engines"
if str(DC_ENGINES_PATH) not in sys.path:
    sys.path.insert(0, str(DC_ENGINES_PATH))

from dc_engines.employee_insight_loop import (  # noqa: E402
    EmployeeInsightProfile,
    EmployeeInsightSessionStatus,
    EmployeeInsightStore,
    PilotStatus,
)


def _load_plugin_module():
    module_path = (
        Path(__file__).resolve().parents[1]
        / "data"
        / "plugins"
        / "employee_insight_plugin"
        / "main.py"
    )
    spec = importlib.util.spec_from_file_location(
        "employee_insight_plugin_test",
        module_path,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _FakeContext:
    def __init__(self, data_dir: Path) -> None:
        self.employee_insight_data_dir = data_dir

    def get_config(self):
        return {}


class _FakeEvent:
    def __init__(self, text: str, sender_id: str = "ou_user") -> None:
        self.message_str = text
        self.unified_msg_origin = f"巅池-Agent小助手:FriendMessage:{sender_id}"
        self.message_obj = SimpleNamespace(type="PrivateMessage")
        self.extras = {}
        self.result = None
        self._sender_id = sender_id

    def get_platform_name(self):
        return "lark"

    def get_platform_id(self):
        return "巅池-Agent小助手"

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return "测试员工"

    def get_group_id(self):
        return ""

    def set_extra(self, key, value):
        self.extras[key] = value

    def set_result(self, result):
        self.result = result


@pytest.mark.asyncio
async def test_employee_insight_plugin_records_private_message_session(tmp_path: Path):
    module = _load_plugin_module()
    plugin = module.EmployeeInsightPlugin(_FakeContext(tmp_path))
    await plugin.initialize()
    store = EmployeeInsightStore(tmp_path / "employee_insight.db")
    await store.upsert_profile(
        EmployeeInsightProfile(
            employee_id="ou_user",
            employee_hash="hash_user",
            pilot_status=PilotStatus.ACTIVE,
            unanswered_outreach_count=2,
            last_outreach_at="2026-06-14T09:00:00Z",
        )
    )
    event = _FakeEvent("帮我写一个培训通知")

    await plugin.on_private_message(event)

    assert event.extras["employee_insight_session_id"]
    session = await store.get_session(event.extras["employee_insight_session_id"])
    assert session is not None
    assert session.channel == "lark_dm"
    assert session.status == EmployeeInsightSessionStatus.ENGAGED
    events = await store.list_events(session.session_id)
    assert [item.event_type for item in events] == [
        "employee_replied",
        "task_started",
    ]
    profile = await store.get_profile("ou_user")
    assert profile.unanswered_outreach_count == 0


@pytest.mark.asyncio
async def test_employee_insight_plugin_handles_pause_as_muted(tmp_path: Path):
    module = _load_plugin_module()
    plugin = module.EmployeeInsightPlugin(_FakeContext(tmp_path))
    await plugin.initialize()
    event = _FakeEvent("暂停")

    await plugin.on_private_message(event)

    store = EmployeeInsightStore(tmp_path / "employee_insight.db")
    session_id = event.extras["employee_insight_session_id"]
    session = await store.get_session(session_id)
    assert session.status == EmployeeInsightSessionStatus.MUTED
    events = await store.list_events(session_id)
    assert events[-1].event_type == "opt_out"


def test_employee_insight_plugin_ignores_group_events(tmp_path: Path):
    module = _load_plugin_module()
    plugin = module.EmployeeInsightPlugin(_FakeContext(tmp_path))
    event = _FakeEvent("帮我写通知")
    event.message_obj.type = "GroupMessage"

    assert plugin._is_private_lark_event(event) is False
