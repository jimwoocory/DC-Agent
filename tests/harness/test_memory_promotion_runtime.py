"""End-to-end guard for the memory promotion chain.

Chain:
  workflow task creation (auto_complete_on_response)
  -> harness_sensor_plugin.on_llm_response
  -> HarnessEngine.complete_task(result={"summary": ...})
  -> HarnessMemoryPromoter.promote_from_task
  -> HarnessMemoryStore (harness_memory.db)

This chain went dormant in production after 2026-06-03: tasks were still
created, but completion and promotion counts dropped to zero. These tests keep
the regression visible before it becomes a silent database gap again.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from dc_engines.harness import (
    HarnessEngine,
    HarnessMemoryPromoter,
    HarnessMemoryStore,
    HarnessTaskStore,
    create_workflow_request,
)

_SENSOR_PLUGIN_PATH = (
    Path(__file__).resolve().parents[2]
    / "data"
    / "plugins"
    / "harness_sensor_plugin"
    / "main.py"
)


def _load_sensor_module():
    spec = importlib.util.spec_from_file_location(
        "dc_harness_sensor_plugin_test",
        _SENSOR_PLUGIN_PATH,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _StubEvent:
    """Minimal event stub exposing only the extras read by the sensor."""

    def __init__(self, task_id: str) -> None:
        self._extras = {"workflow_intent_task_id": task_id}

    def get_extra(self, key: str):
        return self._extras.get(key)


async def _build_engine(tmp_path: Path) -> tuple[HarnessEngine, HarnessMemoryStore]:
    task_store = HarnessTaskStore(str(tmp_path / "harness.db"))
    await task_store.initialize()
    memory_store = HarnessMemoryStore(str(tmp_path / "harness_memory.db"))
    await memory_store.initialize()
    promoter = HarnessMemoryPromoter(memory_store)
    engine = HarnessEngine(task_store, memory_promoter=promoter)
    return engine, memory_store


def _build_sensor(module, engine: HarnessEngine):
    plugin = module.HarnessSensorPlugin.__new__(module.HarnessSensorPlugin)
    plugin.context = SimpleNamespace(harness_engine=engine, ai_inbox_store=None)
    return plugin


async def _create_auto_complete_task(engine: HarnessEngine):
    req = create_workflow_request(
        workflow_kind="project_followup",
        brief="供应链延期风险跟进",
        conversation_id="conv-1",
        platform_id="lark",
        session_id="lark:tenant:user",
        source="workflow_intent_plugin",
        message_text="帮我跟进供应链延期风险",
    )
    req.payload["auto_complete_on_response"] = True
    return await engine.create_task(req)


@pytest.mark.asyncio
async def test_llm_response_completes_task_and_promotes_memory(tmp_path: Path):
    engine, memory_store = await _build_engine(tmp_path)
    module = _load_sensor_module()
    sensor = _build_sensor(module, engine)
    task = await _create_auto_complete_task(engine)

    resp = SimpleNamespace(
        completion_text="供应链延期风险已确认：建议提前备货 30 天，并同步采购部。",
        role="assistant",
    )
    await sensor.maybe_complete_harness_task(_StubEvent(task.task_id), resp)

    settled = await engine.store.get_task(task.task_id)
    assert settled is not None
    assert settled.status == "completed"
    assert settled.result.get("summary", "").strip()
    assert settled.result.get("quality") == "success"

    memories = await memory_store.list_for_session("lark:tenant:user")
    outcome = [m for m in memories if m.memory_kind == "task_outcome"]
    assert len(outcome) == 1
    assert outcome[0].task_id == task.task_id
    assert "供应链" in outcome[0].summary

    events = await engine.store.list_events(task.task_id)
    assert any(e.event_type == "memory_promoted" for e in events)


@pytest.mark.asyncio
async def test_llm_error_response_fails_task_without_memory(tmp_path: Path):
    engine, memory_store = await _build_engine(tmp_path)
    module = _load_sensor_module()
    sensor = _build_sensor(module, engine)
    task = await _create_auto_complete_task(engine)

    resp = SimpleNamespace(
        completion_text="Error code: 500 - upstream provider unavailable",
        role="assistant",
    )
    await sensor.maybe_complete_harness_task(_StubEvent(task.task_id), resp)

    settled = await engine.store.get_task(task.task_id)
    assert settled is not None
    assert settled.status == "failed"

    memories = await memory_store.list_for_session("lark:tenant:user")
    assert [m for m in memories if m.memory_kind == "task_outcome"] == []


@pytest.mark.asyncio
async def test_insufficient_material_response_blocks_task_without_memory(
    tmp_path: Path,
):
    engine, memory_store = await _build_engine(tmp_path)
    module = _load_sensor_module()
    sensor = _build_sensor(module, engine)
    task = await _create_auto_complete_task(engine)

    resp = SimpleNamespace(
        completion_text="资料不足，需要补充供应商合同与排产计划后才能继续。",
        role="assistant",
    )
    await sensor.maybe_complete_harness_task(_StubEvent(task.task_id), resp)

    settled = await engine.store.get_task(task.task_id)
    assert settled is not None
    assert settled.status == "blocked"

    memories = await memory_store.list_for_session("lark:tenant:user")
    assert [m for m in memories if m.memory_kind == "task_outcome"] == []
