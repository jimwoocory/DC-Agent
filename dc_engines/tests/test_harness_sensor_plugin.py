from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace


def _load_harness_sensor_module():
    module_path = (
        Path(__file__).resolve().parents[2]
        / "data"
        / "plugins"
        / "harness_sensor_plugin"
        / "main.py"
    )
    spec = importlib.util.spec_from_file_location(
        "dc_harness_sensor_plugin_test",
        module_path,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _FakeContext:
    def get_config(self):
        return {}


class _FakeEvent:
    def __init__(self, extras: dict[str, object] | None = None) -> None:
        self.extras = extras or {}

    def get_extra(self, key: str):
        return self.extras.get(key)


class _FakeStore:
    def __init__(self, tasks: dict[str, object]) -> None:
        self.tasks = tasks
        self.loaded: list[str] = []

    async def get_task(self, task_id: str):
        self.loaded.append(task_id)
        return self.tasks.get(task_id)


class _FakeEngine:
    def __init__(self, tasks: dict[str, object]) -> None:
        self.store = _FakeStore(tasks)
        self.completed: list[tuple[str, dict]] = []
        self.status_changes: list[tuple[str, str, dict | None]] = []

    async def complete_task(self, task_id: str, *, result: dict):
        self.completed.append((task_id, result))

    async def set_status(
        self,
        task_id: str,
        status: str,
        *,
        event_payload: dict | None = None,
    ):
        self.status_changes.append((task_id, status, event_payload))
        return self.store.tasks[task_id]


class _FakeContextWithEngine(_FakeContext):
    def __init__(self, engine: _FakeEngine) -> None:
        self.harness_engine = engine


async def test_sensor_does_not_fallback_without_event_task_id() -> None:
    module = _load_harness_sensor_module()
    plugin = module.HarnessSensorPlugin(_FakeContext())
    store = _FakeStore(
        {
            "task_1": SimpleNamespace(
                task_id="task_1",
                status="pending",
                payload={"source": "workflow_intent_plugin"},
            )
        }
    )
    engine = SimpleNamespace(store=store)

    assert await plugin._load_target_tasks(_FakeEvent(), engine) == []
    assert store.loaded == []


async def test_sensor_requires_auto_complete_allowed_task() -> None:
    module = _load_harness_sensor_module()
    plugin = module.HarnessSensorPlugin(_FakeContext())
    allowed = SimpleNamespace(
        task_id="allowed",
        status="pending",
        payload={"auto_complete_on_response": True},
    )
    denied = SimpleNamespace(
        task_id="denied",
        status="pending",
        payload={"auto_complete_on_response": False},
    )
    legacy_allowed = SimpleNamespace(
        task_id="legacy_allowed",
        status="pending",
        payload={"source": "workflow_intent_plugin"},
    )
    store = _FakeStore(
        {
            "allowed": allowed,
            "denied": denied,
            "legacy_allowed": legacy_allowed,
        }
    )
    engine = SimpleNamespace(store=store)

    tasks = await plugin._load_target_tasks(
        _FakeEvent(
            {
                "workflow_intent_task_id": [
                    "allowed",
                    "denied",
                    "legacy_allowed",
                ]
            }
        ),
        engine,
    )

    assert [task.task_id for task in tasks] == ["allowed", "legacy_allowed"]


async def test_sensor_excludes_review_required_from_auto_complete() -> None:
    module = _load_harness_sensor_module()
    plugin = module.HarnessSensorPlugin(_FakeContext())
    review_task = SimpleNamespace(
        task_id="review_task",
        status="review_required",
        payload={"auto_complete_on_response": True},
    )
    in_progress_task = SimpleNamespace(
        task_id="in_progress_task",
        status="in_progress",
        payload={"auto_complete_on_response": True},
    )
    store = _FakeStore(
        {
            "review_task": review_task,
            "in_progress_task": in_progress_task,
        }
    )
    engine = SimpleNamespace(store=store)

    tasks = await plugin._load_target_tasks(
        _FakeEvent(
            {
                "workflow_intent_task_id": [
                    "review_task",
                    "in_progress_task",
                ]
            }
        ),
        engine,
        allowed_statuses={"pending", "in_progress"},
    )

    assert [task.task_id for task in tasks] == ["in_progress_task"]


async def test_sensor_settle_skips_review_required_success_response() -> None:
    module = _load_harness_sensor_module()
    task = SimpleNamespace(
        task_id="review_task",
        status="review_required",
        payload={"auto_complete_on_response": True},
    )
    engine = _FakeEngine({"review_task": task})
    plugin = module.HarnessSensorPlugin(_FakeContextWithEngine(engine))

    await plugin._settle_active_tasks(
        _FakeEvent({"workflow_intent_task_id": "review_task"}),
        text="已完成，请查看。",
        quality="success",
        source="harness_sensor_plugin",
        role=None,
    )

    assert engine.completed == []
    assert engine.status_changes == []


async def test_sensor_excludes_review_required_by_default_auto_complete() -> None:
    module = _load_harness_sensor_module()
    plugin = module.HarnessSensorPlugin(_FakeContext())
    review_default = SimpleNamespace(
        task_id="review_default",
        status="in_progress",
        payload={
            "auto_complete_on_response": True,
            "review_required_by_default": True,
        },
    )
    normal = SimpleNamespace(
        task_id="normal",
        status="in_progress",
        payload={"auto_complete_on_response": True},
    )
    store = _FakeStore({"review_default": review_default, "normal": normal})
    engine = SimpleNamespace(store=store)

    tasks = await plugin._load_target_tasks(
        _FakeEvent({"workflow_intent_task_id": ["review_default", "normal"]}),
        engine,
    )

    assert [task.task_id for task in tasks] == ["review_default", "normal"]


def test_sensor_keeps_success_text_with_negated_missing_terms_success() -> None:
    module = _load_harness_sensor_module()

    assert (
        module._classify_response_quality(
            None,
            "已完成，未完成项：无；本次资料无需补充。",
        )
        == "success"
    )
    assert (
        module._classify_response_quality(
            None,
            "fixed the not found fallback and delivered the requested output",
        )
        == "success"
    )
