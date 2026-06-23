from dc_engines.pet_live.contracts import PetEvent, PetSourceRef, PetState
from dc_engines.pet_live.reducer import apply_event


def _event(event_type: str, source: str = "feishu") -> PetEvent:
    return PetEvent(
        pet_id="pet_1",
        user_id="ou_user",
        source=source,
        event_type=event_type,
        source_ref=PetSourceRef(platform="lark", conversation_id="oc_1"),
        payload={},
        created_at="2026-06-15T00:00:00+00:00",
    )


def test_reducer_maps_business_events_to_pet_states() -> None:
    state = PetState(pet_id="pet_1", user_id="ou_user")

    assert apply_event(state, _event("feishu_message_received")).state == "waiting"
    assert apply_event(state, _event("router_decision_made", "router")).state == "thinking"
    assert apply_event(state, _event("harness_task_created", "harness")).state == "working"
    assert apply_event(state, _event("hermes_task_completed", "hermes")).state == "success"
    assert apply_event(state, _event("memory_context_injected", "obsidian")).state == "focused"


def test_reducer_rewards_completed_tasks_without_exceeding_energy_cap() -> None:
    state = PetState(pet_id="pet_1", user_id="ou_user", energy=98, xp=40, coins=2)

    updated = apply_event(state, _event("task_completed", "harness"))

    assert updated.state == "success"
    assert updated.emotion == "happy"
    assert updated.energy == 100
    assert updated.xp == 50
    assert updated.coins == 3


def test_reducer_distillation_events_update_lightweight_state_only() -> None:
    state = PetState(pet_id="pet_1", user_id="emp_001", state="working")
    updated = apply_event(
        state,
        PetEvent(
            pet_id="pet_1",
            user_id="emp_001",
            source="assistant",
            event_type="assistant_distillation_observed",
            source_ref=PetSourceRef(employee_id="emp_001", conversation_id="oc_1"),
            payload={
                "mood": "settled",
                "focus_level": 74,
                "memory_affinity": 61,
                "work_rhythm": "deep_work",
                "summary": "observed stable morning focus",
            },
            created_at="2026-06-15T00:01:00+00:00",
        ),
    )

    assert updated.state == "working"
    assert updated.emotion == state.emotion
    assert updated.mood == "settled"
    assert updated.focus_level == 74
    assert updated.memory_affinity == 61
    assert updated.work_rhythm == "deep_work"
    assert updated.last_signal["event_type"] == "assistant_distillation_observed"


def test_reducer_assistant_message_observed_updates_light_feedback() -> None:
    state = PetState(pet_id="pet_1", user_id="emp_001", state="idle")
    updated = apply_event(
        state,
        PetEvent(
            pet_id="pet_1",
            user_id="emp_001",
            source="feishu_workspace",
            event_type="assistant_message_observed",
            source_ref=PetSourceRef(
                employee_id="emp_001",
                platform="feishu_history",
                conversation_id="oc_1",
                message_id="om_1",
            ),
            payload={"content": "小助手回复内容"},
            created_at="2026-06-15T00:02:00+00:00",
        ),
    )

    assert updated.state == "waiting"
    assert updated.emotion == "curious"
    assert updated.last_signal["event_type"] == "assistant_message_observed"
    assert updated.last_signal["summary"] == "小助手回复内容"
