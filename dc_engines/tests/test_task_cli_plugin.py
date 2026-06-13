from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path


def _load_task_cli_module():
    module_path = (
        Path(__file__).resolve().parents[2]
        / "data"
        / "plugins"
        / "task_cli_plugin"
        / "main.py"
    )
    spec = importlib.util.spec_from_file_location("task_cli_plugin_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@dataclass
class _FakeTask:
    task_id: str = "task-strict"
    title: str = "Strict employee task"
    status: str = "in_progress"
    conversation_id: str = "conv-1"
    domain: str = "department"
    payload: dict | None = None
    result: dict | None = None


class _FakeConversationManager:
    async def get_curr_conversation_id(self, umo: str) -> str:
        return "conv-1"

    async def new_conversation(self, umo: str, platform_id: str) -> str:
        return "conv-1"


class _FakeStore:
    def __init__(self, task: _FakeTask) -> None:
        self.task = task

    async def get_task(self, task_id: str) -> _FakeTask | None:
        if task_id == self.task.task_id:
            return self.task
        return None


class _BlockingEngine:
    def __init__(self) -> None:
        self.completed: list[tuple[str, dict]] = []

    async def complete_task(self, task_id: str, *, result: dict):
        self.completed.append((task_id, result))
        raise RuntimeError(
            "cannot complete employee-facing strict-source task without concrete "
            "source provenance"
        )


class _PassingEngine:
    def __init__(self, task: _FakeTask) -> None:
        self.task = task

    async def complete_task(self, task_id: str, *, result: dict):
        self.task.status = "completed"
        self.task.result = result
        return self.task


@dataclass
class _FakeInboxItem:
    item_id: str = "inbox-1"


class _FakeInboxStore:
    def __init__(self) -> None:
        self.updated: list[dict] = []

    async def find_by_task_id(self, task_id: str) -> _FakeInboxItem:
        return _FakeInboxItem()

    async def update_item(self, item_id: str, **kwargs) -> None:
        self.updated.append({"item_id": item_id, **kwargs})


class _FakeContext:
    def __init__(self, task: _FakeTask, engine) -> None:
        self.conversation_manager = _FakeConversationManager()
        self.harness_store = _FakeStore(task)
        self.harness_engine = engine
        self.ai_inbox_store = _FakeInboxStore()


class _FakeEvent:
    def __init__(self) -> None:
        self.message_str = "/task done task-strict 完成了"
        self.unified_msg_origin = "lark:room"
        self.result = None

    def get_platform_id(self) -> str:
        return "lark"

    def set_result(self, result) -> None:
        self.result = result


async def test_task_done_reports_guard_block_without_closing_inbox() -> None:
    module = _load_task_cli_module()
    task = _FakeTask(payload={"strict_source_provenance_required": True})
    engine = _BlockingEngine()
    context = _FakeContext(task, engine)
    plugin = module.TaskCliPlugin(context)
    event = _FakeEvent()

    await plugin._task_done(event, "task-strict 完成了")

    assert engine.completed == [
        (
            "task-strict",
            {"source": "task_cli_plugin/done", "summary": "完成了"},
        )
    ]
    assert event.result is not None
    assert "任务无法完成" in event.result.get_plain_text()
    assert "source provenance" in event.result.get_plain_text()
    assert context.ai_inbox_store.updated == []


async def test_task_done_closes_inbox_after_real_completion() -> None:
    module = _load_task_cli_module()
    task = _FakeTask()
    context = _FakeContext(task, _PassingEngine(task))
    plugin = module.TaskCliPlugin(context)
    event = _FakeEvent()

    await plugin._task_done(event, "task-strict 真实完成")

    assert event.result is not None
    assert "任务已完成" in event.result.get_plain_text()
    assert context.ai_inbox_store.updated == [
        {
            "item_id": "inbox-1",
            "status": "closed",
            "event_type": "task_closed",
            "event_payload": {"source": "task_cli_plugin/done"},
        }
    ]
