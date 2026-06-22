from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from dc_engines.harness.contracts import HarnessTaskCreateRequest
from dc_engines.harness.engine import HarnessEngine
from dc_engines.harness.loop_runtime import LoopOrchestrator
from dc_engines.harness.runtime_hooks import HarnessSensorRuntime
from dc_engines.harness.task_store import HarnessTaskStore


class _Event:
    def __init__(self, task_id: str) -> None:
        self._extras = {"workflow_intent_task_id": task_id}

    def get_extra(self, key: str) -> Any:
        return self._extras.get(key)


async def _engine(tmp_path: Path) -> HarnessEngine:
    store = HarnessTaskStore(tmp_path / "harness.db")
    await store.initialize()
    return HarnessEngine(store)


def _request(payload: dict | None = None) -> HarnessTaskCreateRequest:
    return HarnessTaskCreateRequest(
        title="Loop settlement task",
        conversation_id="conv_1",
        platform_id="lark",
        session_id="lark:user_1",
        domain="project",
        payload=payload or {"auto_complete_on_response": True},
    )


@pytest.mark.asyncio
async def test_complete_task_records_evidence_settlement(tmp_path: Path) -> None:
    engine = await _engine(tmp_path)
    task = await engine.create_task(_request())
    await engine.mark_in_progress(task.task_id)

    completed = await engine.complete_task(
        task.task_id,
        result={
            "summary": "Delivered with evidence.",
            "evidence": [{"type": "output", "path": "deliverables/brief.md"}],
        },
    )

    events = await engine.store.list_events(task.task_id)
    settlements = [
        event for event in events if event.event_type == "loop_settlement_decided"
    ]
    assert completed.status == "completed"
    assert settlements[-1].payload["status"] == "ok"
    assert settlements[-1].payload["evidence"]


@pytest.mark.asyncio
async def test_complete_task_rejects_empty_evidence(tmp_path: Path) -> None:
    engine = await _engine(tmp_path)
    task = await engine.create_task(_request())
    await engine.mark_in_progress(task.task_id)

    with pytest.raises(RuntimeError, match="evidence"):
        await engine.complete_task(task.task_id, result={"notes": ""})

    events = await engine.store.list_events(task.task_id)
    assert any(event.event_type == "loop_quality_gate_failed" for event in events)
    reloaded = await engine.store.get_task(task.task_id)
    assert reloaded is not None
    assert reloaded.status == "in_progress"


@pytest.mark.asyncio
async def test_terminal_task_rejects_loop_action(tmp_path: Path) -> None:
    engine = await _engine(tmp_path)
    task = await engine.create_task(_request())
    await engine.mark_in_progress(task.task_id)
    await engine.complete_task(
        task.task_id,
        result={"summary": "done", "evidence": [{"type": "summary"}]},
    )

    with pytest.raises(RuntimeError, match="terminal task"):
        await LoopOrchestrator(engine.store).record_action_started(
            task.task_id,
            step_id="tool:late",
            summary="late action",
        )


@pytest.mark.asyncio
async def test_sensor_blocks_with_loop_settlement(tmp_path: Path) -> None:
    engine = await _engine(tmp_path)
    task = await engine.create_task(_request())
    runtime = HarnessSensorRuntime(SimpleNamespace(harness_engine=engine))

    await runtime.settle_active_tasks(
        _Event(task.task_id),
        text="资料不足，需要补充来源。",
        quality="insufficient_materials",
        source="test",
        role=None,
    )

    reloaded = await engine.store.get_task(task.task_id)
    events = await engine.store.list_events(task.task_id)
    settlements = [
        event for event in events if event.event_type == "loop_settlement_decided"
    ]
    assert reloaded is not None
    assert reloaded.status == "blocked"
    assert settlements[-1].payload["metadata"]["blocking_reason"]


@pytest.mark.asyncio
async def test_sensor_routes_review_required_task_to_review_gate(
    tmp_path: Path,
) -> None:
    engine = await _engine(tmp_path)
    task = await engine.create_task(
        _request(
            {
                "auto_complete_on_response": False,
                "review_required_by_default": True,
            }
        )
    )
    runtime = HarnessSensorRuntime(SimpleNamespace(harness_engine=engine))

    await runtime.settle_active_tasks(
        _Event(task.task_id),
        text="这里是待审核交付内容。",
        quality="success",
        source="test",
        role="assistant",
    )

    reloaded = await engine.store.get_task(task.task_id)
    events = await engine.store.list_events(task.task_id)
    settlements = [
        event for event in events if event.event_type == "loop_settlement_decided"
    ]
    assert reloaded is not None
    assert reloaded.status == "review_required"
    assert settlements[-1].payload["metadata"]["review_reason"]
