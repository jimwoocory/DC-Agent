from pathlib import Path

from dc_engines.pet_live.event_bus import publish_pet_event
from dc_engines.pet_live.identity import get_or_create_identity
from dc_engines.pet_live.store import PetLiveStore


def test_publish_pet_event_creates_traceable_event_and_updates_state(
    tmp_path: Path,
) -> None:
    store = PetLiveStore(tmp_path / "pet_live.db")
    identity = get_or_create_identity(store, feishu_open_id="ou_user")

    event = publish_pet_event(
        store,
        pet_id=identity.pet_id,
        user_id="ou_user",
        source="router",
        event_type="router_decision_made",
        source_ref={
            "platform": "lark",
            "conversation_id": "oc_1",
            "router_trace_id": "trace_1",
        },
        payload={"intent": "hermes_escalation"},
        created_at="2026-06-15T00:00:00+00:00",
    )

    state = store.get_pet_state(identity.pet_id)

    assert event.id == 1
    assert event.source_ref.router_trace_id == "trace_1"
    assert state is not None
    assert state.state == "thinking"
    assert state.last_event_id == event.id


def test_publish_distillation_event_updates_snapshot_light_feedback(
    tmp_path: Path,
) -> None:
    store = PetLiveStore(tmp_path / "pet_live.db")
    identity = get_or_create_identity(
        store,
        feishu_open_id="ou_user",
        employee_id="emp_001",
    )

    event = publish_pet_event(
        store,
        pet_id=identity.pet_id,
        user_id="emp_001",
        source="assistant",
        event_type="memory_signal_promoted",
        source_ref={"employee_id": "emp_001", "conversation_id": "oc_1"},
        payload={"memory_affinity": 82, "summary": "promoted reusable project context"},
        created_at="2026-06-15T00:00:00+00:00",
    )

    state = store.get_pet_state(identity.pet_id)

    assert event.state_after is not None
    assert state is not None
    assert state.state == "idle"
    assert state.memory_affinity == 82
    assert state.last_signal["summary"] == "promoted reusable project context"
