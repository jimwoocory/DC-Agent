from pathlib import Path

from dc_engines.harness.contracts import HarnessTaskCreateRequest
from dc_engines.harness.engine import HarnessEngine
from dc_engines.harness.task_store import HarnessTaskStore
from dc_engines.pet_live.identity import get_or_create_identity
from dc_engines.pet_live.store import PetLiveStore


async def test_harness_engine_publishes_pet_events_for_task_lifecycle(
    tmp_path: Path,
) -> None:
    harness_store = HarnessTaskStore(str(tmp_path / "harness.db"))
    await harness_store.initialize()
    pet_store = PetLiveStore(tmp_path / "pet_live.db")
    identity = get_or_create_identity(
        pet_store,
        feishu_open_id="ou_user",
        employee_id="emp_001",
    )
    engine = HarnessEngine(harness_store, pet_live_store=pet_store)

    task = await engine.create_task(
        HarnessTaskCreateRequest(
            title="整理项目待办",
            conversation_id="oc_1",
            platform_id="lark",
            session_id="lark:ou_user",
            domain="project",
            payload={
                "pet_id": identity.pet_id,
                "user_id": "ou_user",
                "employee_id": "emp_001",
            },
        )
    )
    await engine.mark_in_progress(task.task_id)
    await engine.complete_task(task.task_id, result={"summary": "完成"})

    events = pet_store.list_events_after(identity.pet_id, after_id=0)

    assert [event.event_type for event in events] == [
        "harness_task_created",
        "harness_task_in_progress",
        "harness_task_completed",
    ]
    assert all(event.source == "harness" for event in events)
    assert all(event.user_id == "emp_001" for event in events)
    assert events[0].source_ref.employee_id == "emp_001"
    assert events[0].source_ref.harness_task_id == task.task_id
    state = pet_store.get_pet_state(identity.pet_id)
    assert state is not None
    assert state.state == "success"
