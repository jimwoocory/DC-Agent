from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dc_engines"))

import pytest
import pytest_asyncio
from dc_engines.employee_directory.contracts import Employee
from dc_engines.harness.contracts import HarnessTaskCreateRequest
from dc_engines.harness.engine import HarnessEngine
from dc_engines.harness.task_store import HarnessTaskStore


def _load_plugin_class():
    plugin_path = (
        Path(__file__).resolve().parents[1]
        / "data"
        / "plugins"
        / "department_workflow_plugin"
        / "main.py"
    )
    spec = importlib.util.spec_from_file_location(
        "department_workflow_plugin_main",
        plugin_path,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.DepartmentWorkflowPlugin


@pytest_asyncio.fixture
async def harness_engine(tmp_path: Path) -> HarnessEngine:
    store = HarnessTaskStore(str(tmp_path / "harness.db"))
    await store.initialize()
    return HarnessEngine(store)


def _request(payload: dict) -> HarnessTaskCreateRequest:
    return HarnessTaskCreateRequest(
        title="内容 SOP",
        conversation_id="conv_content",
        platform_id="巅池-Agent小助手",
        session_id="lark:chat",
        domain="content_sop:client_dept",
        payload=payload,
    )


class _FakeConversationManager:
    async def get_curr_conversation_id(self, _umo: str) -> str:
        return "conv_department"

    async def new_conversation(self, _umo: str, _platform_id: str) -> str:
        return "conv_department"


class _FakeStore:
    async def list_tasks_for_conversation(self, _conv_id: str, *, limit: int):
        return []


class _FakeExecutionEmployeeStore:
    async def get_employee(self, _open_id: str) -> Employee:
        return Employee(
            open_id="ou_user",
            display_name="蔡挺",
            department="数字化应用部",
            preferences={
                "business_parent_department": "执行部门",
                "department_path": ["总经办", "执行部门", "数字化应用部"],
            },
        )


class _FakeCreateEngine:
    def __init__(self) -> None:
        self.requests: list[HarnessTaskCreateRequest] = []

    async def create_task(self, request: HarnessTaskCreateRequest):
        self.requests.append(request)
        return SimpleNamespace(
            task_id=f"task_{len(self.requests)}",
            payload=request.payload,
            title=request.title,
        )


class _DepartmentWorkflowEvent:
    message_str = "部门工作流 把昨天会议要点整理成正式会议纪要，列出决策和行动项"
    unified_msg_origin = "lark:FriendMessage:ou_user"
    is_at_or_wake_command = False
    message_obj = SimpleNamespace(type="PrivateMessage")

    def __init__(self) -> None:
        self.extra = {}

    def get_platform_id(self) -> str:
        return "lark"

    def get_sender_id(self) -> str:
        return "ou_user"

    def set_extra(self, key: str, value: str) -> None:
        self.extra[key] = value


class _PrivateClientFollowupEvent:
    message_str = (
        "蔡挺：我今天上午要整理五菱端午客户回访素材，下午还要给项目群同步进度。"
        "你帮我先列一个今天的工作优先级，并提醒我哪些内容适合沉淀到知识库。"
    )
    unified_msg_origin = "lark:FriendMessage:ou_user"
    is_at_or_wake_command = False
    message_obj = SimpleNamespace(type="PrivateMessage")

    def __init__(self) -> None:
        self.extra = {}
        self.sent_messages: list[str] = []

    def get_platform_id(self) -> str:
        return "lark"

    def get_sender_id(self) -> str:
        return "ou_user"

    def set_extra(self, key: str, value: str) -> None:
        self.extra[key] = value

    async def send(self, chain) -> None:
        self.sent_messages.append(str(chain))


class _ProposalFrameworkEvent(_PrivateClientFollowupEvent):
    message_str = (
        "灰度测试：中台同事上传给甲方的方案框架后，请小助手基于附件生成一版"
        "给甲方看的提案文案。请输出标题3个、开场说明、方案亮点、执行节奏、"
        "风险提醒、待甲方确认项。必须引用附件作为事实来源，不能编造价格、"
        "承诺、客户身份；只生成内部审稿版，不要直接外发。"
    )


@pytest.mark.asyncio
async def test_ready_content_sop_task_dispatches_to_hermes(
    harness_engine: HarnessEngine,
) -> None:
    dispatched: list[dict] = []

    async def dispatch(task_id, workflow_kind, brief, umo, cognitive_context):
        dispatched.append(
            {
                "task_id": task_id,
                "workflow_kind": workflow_kind,
                "brief": brief,
                "umo": umo,
                "cognitive_context": cognitive_context,
            }
        )
        return True

    plugin_cls = _load_plugin_class()
    plugin = object.__new__(plugin_cls)
    plugin.context = SimpleNamespace(
        harness_engine=harness_engine,
        dispatch_task_to_hermes=dispatch,
    )
    task = await harness_engine.create_task(
        _request(
            {
                "workflow_kind": "content_sop_workflow",
                "brief": "客户内容包",
                "department_id": "client_dept",
                "scenario_id": "client_content_package",
                "content_type": "mixed",
                "lifecycle_stage": "ready_for_generation",
                "generation_allowed": True,
                "source_citations": [{"source_path": "projects/demo.md"}],
                "expected_outputs": [{"key": "message_draft"}],
            }
        )
    )

    await plugin._dispatch_ready_content_sop_task(
        SimpleNamespace(unified_msg_origin="lark:chat"),
        task,
    )

    reloaded = await harness_engine.store.get_task(task.task_id)
    assert reloaded is not None
    assert reloaded.status == "in_progress"
    assert dispatched[0]["workflow_kind"] == "content_sop_workflow"
    assert dispatched[0]["cognitive_context"]["department_id"] == "client_dept"


@pytest.mark.asyncio
async def test_department_workflow_plugin_does_not_override_auto_complete_gate() -> (
    None
):
    engine = _FakeCreateEngine()
    plugin_cls = _load_plugin_class()
    plugin = plugin_cls(
        SimpleNamespace(
            harness_engine=engine,
            harness_store=_FakeStore(),
            conversation_manager=_FakeConversationManager(),
            employee_store=None,
            case_engine=None,
        ),
        {
            "enabled": True,
            "dry_run": False,
            "notify_on_match": False,
        },
    )

    await plugin.on_message(_DepartmentWorkflowEvent())

    assert len(engine.requests) == 1
    payload = engine.requests[0].payload
    assert payload["workflow_kind"] == "department_workflow"
    assert payload["review_required_by_default"] is True
    assert payload["auto_complete_on_response"] is False


@pytest.mark.asyncio
async def test_department_workflow_task_contains_runtime_principal() -> None:
    engine = _FakeCreateEngine()
    plugin_cls = _load_plugin_class()
    plugin = plugin_cls(
        SimpleNamespace(
            harness_engine=engine,
            harness_store=_FakeStore(),
            conversation_manager=_FakeConversationManager(),
            employee_store=None,
            dc_permission_store=None,
            case_engine=None,
        ),
        {
            "enabled": True,
            "dry_run": False,
            "notify_on_match": False,
        },
    )

    await plugin.on_message(_DepartmentWorkflowEvent())

    payload = engine.requests[0].payload
    assert payload["requester_open_id"] == "ou_user"
    assert payload["requester_identity_source"] == "event_sender"
    assert payload["requester_principal_type"] == "user"
    assert {item["permission"] for item in payload["requester_dc_permissions"]} == {
        "employee_self_service"
    }


@pytest.mark.asyncio
async def test_department_workflow_private_advisory_match_is_observe_only_by_default() -> (
    None
):
    engine = _FakeCreateEngine()
    plugin_cls = _load_plugin_class()
    plugin = plugin_cls(
        SimpleNamespace(
            harness_engine=engine,
            harness_store=_FakeStore(),
            conversation_manager=_FakeConversationManager(),
            employee_store=None,
            case_engine=None,
        ),
        {
            "enabled": True,
            "dry_run": False,
            "notify_on_match": True,
        },
    )
    event = _PrivateClientFollowupEvent()

    await plugin.on_message(event)

    assert len(engine.requests) == 0
    assert event.sent_messages == []
    assert event.extra["department_workflow_candidate"]["mode"] == "observe_only"


@pytest.mark.asyncio
async def test_department_workflow_implicit_task_creation_requires_config() -> None:
    engine = _FakeCreateEngine()
    plugin_cls = _load_plugin_class()
    plugin = plugin_cls(
        SimpleNamespace(
            harness_engine=engine,
            harness_store=_FakeStore(),
            conversation_manager=_FakeConversationManager(),
            employee_store=None,
            case_engine=None,
        ),
        {
            "enabled": True,
            "dry_run": False,
            "notify_on_match": True,
            "implicit_create_tasks": True,
        },
    )
    event = _PrivateClientFollowupEvent()

    await plugin.on_message(event)

    assert len(engine.requests) == 1
    assert event.sent_messages == []


@pytest.mark.asyncio
async def test_proposal_framework_request_is_observe_only_for_execution_employee() -> (
    None
):
    engine = _FakeCreateEngine()
    plugin_cls = _load_plugin_class()
    plugin = plugin_cls(
        SimpleNamespace(
            harness_engine=engine,
            harness_store=_FakeStore(),
            conversation_manager=_FakeConversationManager(),
            employee_store=_FakeExecutionEmployeeStore(),
            case_engine=None,
        ),
        {
            "enabled": True,
            "dry_run": False,
            "notify_on_match": True,
        },
    )
    event = _ProposalFrameworkEvent()

    await plugin.on_message(event)

    assert len(engine.requests) == 0
    assert event.sent_messages == []
    assert event.extra["department_workflow_candidate"]["department_id"] in {
        "execution_ops",
        "client_dept",
    }


@pytest.mark.asyncio
async def test_needs_materials_content_sop_task_does_not_dispatch(
    harness_engine: HarnessEngine,
) -> None:
    async def dispatch(*args, **kwargs):
        raise AssertionError("needs_materials task must not dispatch to Hermes")

    plugin_cls = _load_plugin_class()
    plugin = object.__new__(plugin_cls)
    plugin.context = SimpleNamespace(
        harness_engine=harness_engine,
        dispatch_task_to_hermes=dispatch,
    )
    task = await harness_engine.create_task(
        _request(
            {
                "workflow_kind": "content_sop_workflow",
                "brief": "帮我写客户邀约文案并配图",
                "lifecycle_stage": "needs_materials",
                "generation_allowed": False,
                "missing_required_inputs": [{"key": "audience", "label": "客户"}],
            }
        )
    )

    await plugin._dispatch_ready_content_sop_task(
        SimpleNamespace(unified_msg_origin="lark:chat"),
        task,
    )

    events = await harness_engine.store.list_events(task.task_id)
    assert any(
        event.event_type == "content_sop_material_intake_required" for event in events
    )
