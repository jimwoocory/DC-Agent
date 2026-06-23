from pathlib import Path

import pytest

from dc_engines.pet_live.identity import get_or_create_identity
from dc_engines.pet_live.store import PetLiveStore
from harness.hermes_bridge import HermesBridge, HermesTaskRequest


def _request(pet_id: str) -> HermesTaskRequest:
    return HermesTaskRequest(
        router_decision={"intent": "hermes_escalation"},
        user_input="整理项目",
        queue_job_id="job_1",
        payload={
            "session_id": "lark:ou_user",
            "pet_id": pet_id,
            "user_id": "ou_user",
            "employee_id": "emp_001",
        },
        target_runtime="codex_cli",
    )


@pytest.mark.asyncio
async def test_hermes_bridge_publishes_pet_events_for_callbacks(
    tmp_path: Path,
) -> None:
    pet_store = PetLiveStore(tmp_path / "pet_live.db")
    identity = get_or_create_identity(
        pet_store,
        feishu_open_id="ou_user",
        employee_id="emp_001",
    )
    bridge = HermesBridge(pet_live_store=pet_store)
    request = _request(identity.pet_id)

    await bridge._notify_running(request, "codex_cli")
    await bridge._finish_completed(
        request,
        {"text": "完成", "runtime": "codex_cli", "source_citations": [{"id": "src"}]},
    )
    await bridge._finish_failed(request, "boom")

    events = pet_store.list_events_after(identity.pet_id, after_id=0)

    assert [event.event_type for event in events] == [
        "hermes_task_running",
        "hermes_task_completed",
        "hermes_task_failed",
    ]
    assert events[0].source_ref.hermes_job_id == "job_1"
    assert events[0].source_ref.employee_id == "emp_001"
    assert events[0].user_id == "emp_001"
    assert events[0].payload["runtime"] == "codex_cli"
