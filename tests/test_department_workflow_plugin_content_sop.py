from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
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
