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
