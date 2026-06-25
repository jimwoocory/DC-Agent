from __future__ import annotations

from pathlib import Path

import pytest
from dc_engines.harness.contracts import HarnessTaskCreateRequest
from dc_engines.harness.engine import HarnessEngine
from dc_engines.harness.loop_runtime import LoopOrchestrator
from dc_engines.harness.task_store import HarnessTaskStore
from fastapi import FastAPI

from astrbot.dashboard.asgi_runtime import FastAPIAppAdapter
from astrbot.dashboard.routes.harness_loop import HarnessLoopRoute
from astrbot.dashboard.routes.route import RouteContext


def _build_harness_loop_test_app(tmp_path: Path) -> FastAPIAppAdapter:
    app = FastAPIAppAdapter(FastAPI())
    HarnessLoopRoute(RouteContext(config={}, app=app), dc_root=tmp_path)
    return app


@pytest.mark.asyncio
async def test_harness_loop_tasks_and_timeline_routes(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    store = HarnessTaskStore(data_dir / "harness.db")
    await store.initialize()
    engine = HarnessEngine(store)
    task = await engine.create_task(
        HarnessTaskCreateRequest(
            title="Route loop task",
            conversation_id="conv_1",
            platform_id="lark",
            session_id="lark:user_1",
            domain="project",
            payload={"auto_complete_on_response": True},
        )
    )
    await LoopOrchestrator(store).record_action_started(
        task.task_id,
        step_id="route:test",
        summary="Route test action.",
    )

    client = _build_harness_loop_test_app(tmp_path).test_client()
    task_response = await (await client.get("/api/harness-loop/tasks")).get_json()
    timeline_response = await (
        await client.get(f"/api/harness-loop/tasks/{task.task_id}/timeline")
    ).get_json()

    assert task_response["status"] == "ok"
    assert task_response["data"]["tasks"][0]["task_id"] == task.task_id
    assert timeline_response["status"] == "ok"
    event_types = [
        event["event_type"] for event in timeline_response["data"]["timeline"]
    ]
    assert event_types.index("loop_plan_created") < event_types.index(
        "loop_action_started"
    )


@pytest.mark.asyncio
async def test_harness_loop_tasks_filter_status(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    store = HarnessTaskStore(data_dir / "harness.db")
    await store.initialize()
    engine = HarnessEngine(store)
    task = await engine.create_task(
        HarnessTaskCreateRequest(
            title="Filtered task",
            conversation_id="conv_1",
            platform_id="lark",
            session_id="lark:user_1",
            domain="project",
            payload={"auto_complete_on_response": True},
        )
    )
    await engine.mark_in_progress(task.task_id)

    client = _build_harness_loop_test_app(tmp_path).test_client()
    matched = await (
        await client.get("/api/harness-loop/tasks?status=in_progress")
    ).get_json()
    skipped = await (
        await client.get("/api/harness-loop/tasks?status=completed")
    ).get_json()

    assert matched["data"]["tasks"][0]["task_id"] == task.task_id
    assert skipped["data"]["tasks"] == []
