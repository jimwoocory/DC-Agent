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
        "dc_harness_sensor_plugin_truth_intake_test",
        module_path,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _FakeEvent:
    def __init__(self, extras: dict[str, object] | None = None) -> None:
        self.extras = extras or {}

    def get_extra(self, key: str):
        return self.extras.get(key)


class _FakeStore:
    def __init__(self, tasks: dict[str, object]) -> None:
        self.tasks = tasks

    async def get_task(self, task_id: str):
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


class _FakeContextWithEngine:
    def __init__(self, engine: _FakeEngine) -> None:
        self.harness_engine = engine

    def get_config(self):
        return {}


def test_sensor_classifies_missing_materials_as_insufficient() -> None:
    module = _load_harness_sensor_module()

    quality = module._classify_response_quality(
        None,
        "我无法确认这个文件里的真实信息，还需要补充原始资料后才能继续。",
    )

    assert quality == "insufficient_materials"


async def test_sensor_blocks_instead_of_completes_when_materials_are_insufficient() -> (
    None
):
    module = _load_harness_sensor_module()
    task = SimpleNamespace(
        task_id="truth_task",
        status="in_progress",
        payload={"source": "llm_router_truth_intake"},
    )
    engine = _FakeEngine({"truth_task": task})
    plugin = module.HarnessSensorPlugin(_FakeContextWithEngine(engine))

    await plugin._settle_active_tasks(
        _FakeEvent({"dc_truth_intake_task_id": "truth_task"}),
        text="我没读到本次附件，资料不足，无法确认真实内容。",
        quality="insufficient_materials",
        source="harness_sensor_plugin",
        role=None,
    )

    assert engine.completed == []
    assert engine.status_changes == [
        (
            "truth_task",
            "blocked",
            {
                "reason": "insufficient_source_materials",
                "response_preview": "我没读到本次附件，资料不足，无法确认真实内容。",
                "source": "harness_sensor_plugin",
            },
        )
    ]
