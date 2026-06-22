from __future__ import annotations

from pathlib import Path

import pytest
from dc_engines.harness.contracts import HarnessTaskCreateRequest
from dc_engines.harness.engine import HarnessEngine
from dc_engines.harness.loop_runtime import LOOP_VERSION, LoopOrchestrator
from dc_engines.harness.task_store import HarnessTaskStore


async def _engine(tmp_path: Path) -> HarnessEngine:
    store = HarnessTaskStore(tmp_path / "harness.db")
    await store.initialize()
    return HarnessEngine(store)


def _request(payload: dict | None = None) -> HarnessTaskCreateRequest:
    return HarnessTaskCreateRequest(
        title="Loop task",
        conversation_id="conv_1",
        platform_id="lark",
        session_id="lark:user_1",
        domain="project",
        payload=payload or {"workflow_kind": "project_followup"},
    )


@pytest.mark.asyncio
async def test_create_task_records_loop_plan(tmp_path: Path) -> None:
    engine = await _engine(tmp_path)

    task = await engine.create_task(_request())
    events = await engine.store.list_events(task.task_id)
    plan_events = [event for event in events if event.event_type == "loop_plan_created"]

    assert len(plan_events) == 1
    payload = plan_events[0].payload
    assert payload["loop_version"] == LOOP_VERSION
    assert payload["kind"] == "plan"
    assert payload["step_id"] == "plan:created"
    assert payload["metadata"]["steps"]


@pytest.mark.asyncio
async def test_loop_orchestrator_records_action_observation_and_decision(
    tmp_path: Path,
) -> None:
    engine = await _engine(tmp_path)
    task = await engine.create_task(_request())
    orchestrator = LoopOrchestrator(engine.store)

    await orchestrator.record_action_started(
        task.task_id,
        step_id="tool:kb-query",
        summary="Query knowledge base.",
        metadata={"tool": "kb"},
    )
    await orchestrator.record_observation(
        task.task_id,
        step_id="tool:kb-query:result",
        summary="Knowledge base returned one hit.",
        evidence=[{"type": "hit", "id": "doc_1"}],
    )
    await orchestrator.record_decision(
        task.task_id,
        step_id="decision:continue",
        summary="Continue to settlement.",
    )

    event_types = [
        event.event_type for event in await engine.store.list_events(task.task_id)
    ]
    assert "loop_action_started" in event_types
    assert "loop_observation_recorded" in event_types
    assert "loop_decision_recorded" in event_types
