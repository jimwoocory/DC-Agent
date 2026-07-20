from __future__ import annotations

import asyncio
import importlib.util
import sys
import time
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
    TaskStatus,
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
    def __init__(
        self,
        data_dir: Path,
        employee_store=None,
        config: dict | None = None,
    ) -> None:
        self.employee_insight_data_dir = data_dir
        self.cron_job_manager = None
        self.employee_store = employee_store
        self._config = config or {}

    def get_config(self):
        return self._config


class _FakeCronJobManager:
    def __init__(self) -> None:
        self.jobs = []

    async def add_basic_job(self, **kwargs):
        self.jobs.append(kwargs)
        return SimpleNamespace(job_id="job_employee_insight_daily")


class _FakeConfiguredContext(_FakeContext):
    def __init__(self, data_dir: Path, config: dict) -> None:
        super().__init__(data_dir, config=config)
        self._config = config
        self.cron_job_manager = _FakeCronJobManager()

    def get_config(self):
        return self._config


class _FakeEmployeeStore:
    def __init__(self, employees: dict[str, SimpleNamespace]) -> None:
        self.employees = employees

    async def get_employee(self, open_id: str):
        return self.employees.get(open_id)


class _FakeEvent:
    def __init__(
        self,
        text: str,
        sender_id: str = "ou_user",
        *,
        platform_name: str = "lark",
        platform_id: str = "巅池-Agent小助手",
        private_chat: bool = True,
    ) -> None:
        self.message_str = text
        self.unified_msg_origin = f"{platform_id}:FriendMessage:{sender_id}"
        self.message_obj = SimpleNamespace(type="PrivateMessage")
        self.extras = {}
        self.result = None
        self._sender_id = sender_id
        self._platform_name = platform_name
        self._platform_id = platform_id
        self._private_chat = private_chat
        self._group_id = ""

    def get_platform_name(self):
        return self._platform_name

    def get_platform_id(self):
        return self._platform_id

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return "测试员工"

    def get_group_id(self):
        return self._group_id

    def is_private_chat(self):
        return self._private_chat

    def set_extra(self, key, value):
        self.extras[key] = value

    def get_extra(self, key):
        return self.extras.get(key)

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
async def test_employee_insight_plugin_ignores_non_task_private_messages(
    tmp_path: Path,
):
    module = _load_plugin_module()
    plugin = module.EmployeeInsightPlugin(_FakeContext(tmp_path))
    await plugin.initialize()
    store = EmployeeInsightStore(tmp_path / "employee_insight.db")
    await store.upsert_profile(
        EmployeeInsightProfile(
            employee_id="ou_user",
            employee_hash="hash_user",
            pilot_status=PilotStatus.ACTIVE,
        )
    )

    await plugin.on_private_message(_FakeEvent("你好，小助手"))
    await plugin.on_private_message(_FakeEvent("/new session"))
    await plugin.on_private_message(_FakeEvent('__card_action__:{"value":{}}'))

    assert await store.list_sessions() == []


@pytest.mark.asyncio
async def test_employee_insight_plugin_attaches_negative_feedback_to_active_task(
    tmp_path: Path,
):
    module = _load_plugin_module()
    plugin = module.EmployeeInsightPlugin(_FakeContext(tmp_path))
    await plugin.initialize()
    store = EmployeeInsightStore(tmp_path / "employee_insight.db")
    await store.upsert_profile(
        EmployeeInsightProfile(
            employee_id="ou_user",
            employee_hash="hash_user",
            pilot_status=PilotStatus.ACTIVE,
        )
    )
    first_event = _FakeEvent("帮我写一个培训通知")
    await plugin.on_private_message(first_event)
    original_session_id = first_event.extras["employee_insight_session_id"]

    feedback_event = _FakeEvent("这个结果不行，太慢了，也没解决我的问题")
    await plugin.on_private_message(feedback_event)

    sessions = await store.list_sessions()
    assert len(sessions) == 1
    session = sessions[0]
    assert feedback_event.extras["employee_insight_session_id"] == original_session_id
    assert session.status == EmployeeInsightSessionStatus.BLOCKED
    assert session.task_status == TaskStatus.BLOCKED
    assert session.satisfaction == "negative"
    assert session.friction_points == ["这个结果不行，太慢了，也没解决我的问题"]
    events = await store.list_events(original_session_id)
    assert events[-1].event_type == "friction_reported"


@pytest.mark.asyncio
async def test_employee_insight_plugin_links_harness_task_to_session(tmp_path: Path):
    module = _load_plugin_module()
    plugin = module.EmployeeInsightPlugin(_FakeContext(tmp_path))
    await plugin.initialize()
    store = EmployeeInsightStore(tmp_path / "employee_insight.db")
    await store.upsert_profile(
        EmployeeInsightProfile(
            employee_id="ou_user",
            employee_hash="hash_user",
            pilot_status=PilotStatus.ACTIVE,
        )
    )
    event = _FakeEvent("帮我整理项目计划")
    await plugin.on_private_message(event)

    await plugin.link_task_lifecycle(event, "task_123", source="test")

    session = await store.get_session(event.extras["employee_insight_session_id"])
    assert session is not None
    assert session.task_status == TaskStatus.RUNNING
    assert session.metadata["harness_task_id"] == "task_123"
    events = await store.list_events(session.session_id)
    assert events[-1].event_type == "task_linked"


@pytest.mark.asyncio
async def test_employee_insight_plugin_closes_session_from_harness_lifecycle(
    tmp_path: Path,
):
    module = _load_plugin_module()
    plugin = module.EmployeeInsightPlugin(_FakeContext(tmp_path))
    await plugin.initialize()
    store = EmployeeInsightStore(tmp_path / "employee_insight.db")
    await store.upsert_profile(
        EmployeeInsightProfile(
            employee_id="ou_user",
            employee_hash="hash_user",
            pilot_status=PilotStatus.ACTIVE,
        )
    )
    event = _FakeEvent("帮我整理项目计划")
    await plugin.on_private_message(event)
    await plugin.link_task_lifecycle(event, "task_123", source="test")

    await plugin.update_task_lifecycle(
        "task_123",
        status="delivered",
        source="harness_sensor",
    )
    await plugin.update_task_lifecycle(
        "task_123",
        status="closed",
        source="task_cli",
    )

    session = await store.get_session(event.extras["employee_insight_session_id"])
    assert session is not None
    assert session.status == EmployeeInsightSessionStatus.COMPLETED
    assert session.task_status == TaskStatus.COMPLETED
    events = await store.list_events(session.session_id)
    assert [item.event_type for item in events[-2:]] == [
        "task_result_delivered",
        "task_completed",
    ]


@pytest.mark.asyncio
async def test_employee_insight_plugin_distills_delivered_task_once(
    tmp_path: Path,
) -> None:
    module = _load_plugin_module()
    plugin = module.EmployeeInsightPlugin(_FakeContext(tmp_path))
    await plugin.initialize()
    store = EmployeeInsightStore(tmp_path / "employee_insight.db")
    await store.upsert_profile(
        EmployeeInsightProfile(
            employee_id="ou_user",
            employee_hash="hash_user",
            pilot_status=PilotStatus.ACTIVE,
        )
    )
    event = _FakeEvent("帮我整理项目计划")
    await plugin.on_private_message(event)
    await plugin.link_task_lifecycle(event, "task_123", source="test")

    await plugin.update_task_lifecycle(
        "task_123",
        status="delivered",
        source="harness_sensor",
    )
    await plugin.update_task_lifecycle(
        "task_123",
        status="closed",
        source="task_cli",
    )

    session_id = event.extras["employee_insight_session_id"]
    candidates = await store.list_candidates()
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.source_session_ids == [session_id]
    assert candidate.review_status.value == "review_required"
    assert candidate.is_runtime_eligible is False
    assert candidate.obsidian_note_path
    assert (tmp_path / candidate.obsidian_note_path).exists()
    audits = await store.list_audit_events(candidate.candidate_id)
    assert [item.action for item in audits] == [
        "candidate_created",
        "governance_exported",
    ]


@pytest.mark.asyncio
async def test_employee_insight_plugin_auto_observes_new_private_message_sender(
    tmp_path: Path,
):
    module = _load_plugin_module()
    plugin = module.EmployeeInsightPlugin(
        _FakeContext(tmp_path, config={"auto_observe_private_dm": True})
    )
    await plugin.initialize()
    event = _FakeEvent("我不知道这个系统怎么用", sender_id="ou_new_employee")

    await plugin.on_private_message(event)

    store = EmployeeInsightStore(tmp_path / "employee_insight.db")
    profile = await store.get_profile("ou_new_employee")
    assert profile is not None
    assert profile.pilot_status == PilotStatus.ACTIVE
    assert profile.role == "自然私聊观察"
    assert profile.metadata["observed_from_dm"] is True
    assert profile.metadata["verification_scope"] is True
    assert event.result is None
    audit = await store.list_audit_events("ou_new_employee")
    assert audit[-1].action == "profile_observed_from_lark_dm"


@pytest.mark.asyncio
async def test_employee_insight_plugin_enriches_profile_and_session_from_directory(
    tmp_path: Path,
):
    module = _load_plugin_module()
    employee_store = _FakeEmployeeStore(
        {
            "ou_activity": SimpleNamespace(
                display_name="肖焕辉",
                department="活动统筹部",
                role="活动统筹",
            )
        }
    )
    plugin = module.EmployeeInsightPlugin(
        _FakeContext(
            tmp_path,
            employee_store,
            config={"auto_observe_private_dm": True},
        )
    )
    await plugin.initialize()
    event = _FakeEvent("我想整理一下这次活动执行的需求", sender_id="ou_activity")

    await plugin.on_private_message(event)

    store = EmployeeInsightStore(tmp_path / "employee_insight.db")
    profile = await store.get_profile("ou_activity")
    assert profile is not None
    assert profile.display_name == "肖焕辉"
    assert profile.department_id == "活动统筹部"
    assert profile.role == "活动统筹"
    assert profile.metadata["directory_source"] == "employees_db"
    session = await store.get_session(event.extras["employee_insight_session_id"])
    assert session is not None
    assert session.department_id == "活动统筹部"
    assert session.role == "活动统筹"
    assert session.metadata["directory_display_name"] == "肖焕辉"


@pytest.mark.asyncio
async def test_employee_insight_plugin_handles_pause_as_muted(tmp_path: Path):
    module = _load_plugin_module()
    plugin = module.EmployeeInsightPlugin(_FakeContext(tmp_path))
    await plugin.initialize()
    store = EmployeeInsightStore(tmp_path / "employee_insight.db")
    await store.upsert_profile(
        EmployeeInsightProfile(
            employee_id="ou_user",
            employee_hash="hash_user",
            pilot_status=PilotStatus.ACTIVE,
        )
    )
    event = _FakeEvent("暂停")

    await plugin.on_private_message(event)

    session_id = event.extras["employee_insight_session_id"]
    session = await store.get_session(session_id)
    assert session.status == EmployeeInsightSessionStatus.MUTED
    events = await store.list_events(session_id)
    assert events[-1].event_type == "opt_out"


@pytest.mark.asyncio
async def test_employee_insight_plugin_pilot_only_skips_unknown_private_message(
    tmp_path: Path,
):
    module = _load_plugin_module()
    plugin = module.EmployeeInsightPlugin(_FakeContext(tmp_path))
    await plugin.initialize()
    event = _FakeEvent("帮我写一个培训通知", sender_id="ou_not_in_pilot")

    await plugin.on_private_message(event)

    store = EmployeeInsightStore(tmp_path / "employee_insight.db")
    assert await store.get_profile("ou_not_in_pilot") is None
    assert "employee_insight_session_id" not in event.extras
    assert event.extras["employee_insight_candidate"] == {
        "mode": "observe_only",
        "reason": "pilot_only",
    }
    assert event.result is None


@pytest.mark.asyncio
async def test_employee_insight_plugin_side_effect_timeout_does_not_block_chat(
    tmp_path: Path,
):
    module = _load_plugin_module()
    plugin = module.EmployeeInsightPlugin(
        _FakeContext(tmp_path, config={"side_effect_timeout_seconds": 0.01})
    )
    await plugin.initialize()
    event = _FakeEvent("帮我写一个培训通知", sender_id="ou_timeout")

    async def slow_handle(*args, **kwargs):
        await asyncio.sleep(1)

    plugin._handle_private_message = slow_handle
    start = time.monotonic()

    await plugin.on_private_message(event)

    assert time.monotonic() - start < 0.5
    assert event.extras == {}
    assert event.result is None


@pytest.mark.asyncio
async def test_employee_insight_plugin_registers_verification_profile_from_private_message(
    tmp_path: Path,
):
    module = _load_plugin_module()
    plugin = module.EmployeeInsightPlugin(_FakeContext(tmp_path))
    await plugin.initialize()
    event = _FakeEvent("加入灰度验证", sender_id="ou_self_test")

    await plugin.on_private_message(event)

    store = EmployeeInsightStore(tmp_path / "employee_insight.db")
    profile = await store.get_profile("ou_self_test")
    assert profile is not None
    assert profile.pilot_status == PilotStatus.ACTIVE
    assert profile.metadata["verification_scope"] is True
    audit = await store.list_audit_events("ou_self_test")
    assert audit[-1].action == "verification_profile_registered"
    assert "灰度验证测试名单" in event.result.chain[0].text


@pytest.mark.asyncio
async def test_employee_insight_plugin_accepts_display_named_lark_adapter(
    tmp_path: Path,
):
    module = _load_plugin_module()
    plugin = module.EmployeeInsightPlugin(_FakeContext(tmp_path))
    await plugin.initialize()
    event = _FakeEvent(
        "加入灰度验证",
        sender_id="ou_display_name",
        platform_name="巅池-Agent小助手",
        platform_id="巅池-Agent小助手",
    )

    await plugin.on_private_message(event)

    store = EmployeeInsightStore(tmp_path / "employee_insight.db")
    profile = await store.get_profile("ou_display_name")
    assert profile is not None
    assert profile.pilot_status == PilotStatus.ACTIVE
    assert profile.metadata["self_registered"] is True
    assert "灰度验证测试名单" in event.result.chain[0].text


def test_employee_insight_plugin_ignores_group_events(tmp_path: Path):
    module = _load_plugin_module()
    plugin = module.EmployeeInsightPlugin(_FakeContext(tmp_path))
    event = _FakeEvent("帮我写通知", private_chat=False)
    event.message_obj.type = "GroupMessage"
    event._group_id = "oc_group"

    assert plugin._is_private_lark_event(event) is False


def test_employee_insight_plugin_accepts_lark_p2p_chat_id_shape(tmp_path: Path):
    module = _load_plugin_module()
    plugin = module.EmployeeInsightPlugin(_FakeContext(tmp_path))
    event = _FakeEvent("我不会用这个系统")
    event.message_obj.chat_id = "oc_p2p_chat_id"

    assert plugin._is_private_lark_event(event) is True


@pytest.mark.asyncio
async def test_employee_insight_plugin_registers_daily_outreach_cron(
    tmp_path: Path,
) -> None:
    module = _load_plugin_module()
    context = _FakeConfiguredContext(
        tmp_path,
        {
            "daily_outreach_enabled": True,
            "daily_outreach_cron": "0 10 * * *",
            "daily_outreach_timezone": "Asia/Shanghai",
        },
    )
    plugin = module.EmployeeInsightPlugin(context)

    await plugin.initialize()

    assert context.cron_job_manager.jobs
    job = context.cron_job_manager.jobs[0]
    assert job["name"] == "员工需求洞察每日触达"
    assert job["cron_expression"] == "0 10 * * *"
    assert job["persistent"] is True
