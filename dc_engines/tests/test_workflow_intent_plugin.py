from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


def _load_workflow_intent_module():
    module_path = (
        Path(__file__).resolve().parents[2]
        / "data"
        / "plugins"
        / "workflow_intent_plugin"
        / "main.py"
    )
    spec = importlib.util.spec_from_file_location(
        "dc_workflow_intent_plugin_test",
        module_path,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _FakeContext:
    harness_engine = object()
    harness_store = object()


class _FakeEvent:
    message_str = "下个季度的营销计划要赶紧搞起来"
    unified_msg_origin = "lark:FriendMessage:ou_user"
    is_at_or_wake_command = False


async def test_workflow_intent_can_delegate_to_department_workflow() -> None:
    module = _load_workflow_intent_module()
    engine = _FakeEngine()
    plugin = module.WorkflowIntentPlugin(
        _workflow_context(engine),
        {"delegate_to_department_workflow": True},
    )

    await plugin.on_message(_WorkflowEvent("下个季度的营销计划要赶紧搞起来"))

    assert engine.requests == []


class _FakeConversationManager:
    async def get_curr_conversation_id(self, _umo: str) -> str:
        return "conv_workflow"

    async def new_conversation(self, _umo: str, _platform_id: str) -> str:
        return "conv_workflow"


class _FakeStore:
    async def list_tasks_for_conversation(self, _conv_id: str, *, limit: int):
        return []


class _FakeEngine:
    def __init__(self) -> None:
        self.requests = []

    async def create_task(self, request):
        self.requests.append(request)
        return SimpleNamespace(task_id=f"task_{len(self.requests)}")


class _WorkflowEvent:
    unified_msg_origin = "lark:FriendMessage:ou_user"
    is_at_or_wake_command = False
    message_obj = SimpleNamespace(type="PrivateMessage")

    def __init__(self, message: str) -> None:
        self.message_str = message
        self.extra = {}

    def get_platform_id(self) -> str:
        return "lark"

    def get_sender_id(self) -> str:
        return "ou_user"

    def set_extra(self, key: str, value: str) -> None:
        self.extra[key] = value


def _workflow_context(engine: _FakeEngine):
    return SimpleNamespace(
        harness_engine=engine,
        harness_store=_FakeStore(),
        conversation_manager=_FakeConversationManager(),
        employee_store=None,
        case_engine=None,
    )


@pytest.mark.asyncio
async def test_workflow_intent_review_required_kind_disables_auto_complete() -> None:
    module = _load_workflow_intent_module()
    engine = _FakeEngine()
    plugin = module.WorkflowIntentPlugin(_workflow_context(engine), {})

    await plugin.on_message(_WorkflowEvent("下个季度的营销计划要赶紧搞起来"))

    assert len(engine.requests) == 1
    payload = engine.requests[0].payload
    assert payload["workflow_kind"] == "marketing_plan"
    assert payload["review_required_by_default"] is True
    assert payload["auto_complete_on_response"] is False


@pytest.mark.asyncio
async def test_workflow_intent_project_followup_auto_completes_when_allowed() -> None:
    module = _load_workflow_intent_module()
    engine = _FakeEngine()
    plugin = module.WorkflowIntentPlugin(_workflow_context(engine), {})

    await plugin.on_message(_WorkflowEvent("今天的项目跟进汇报同步一下"))

    assert len(engine.requests) == 1
    payload = engine.requests[0].payload
    assert payload["workflow_kind"] == "project_followup"
    assert payload["review_required_by_default"] is False
    assert payload["auto_complete_on_response"] is True
