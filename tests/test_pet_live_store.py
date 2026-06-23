from pathlib import Path

from dc_engines.pet_live.contracts import PetEvent, PetSourceRef
from dc_engines.pet_live.identity import get_or_create_identity
from dc_engines.pet_live.store import PetLiveStore


def test_store_persists_event_outbox_with_state_snapshots(tmp_path: Path) -> None:
    store = PetLiveStore(tmp_path / "pet_live.db")
    identity = get_or_create_identity(store, feishu_open_id="ou_user")

    stored = store.append_event(
        PetEvent(
            pet_id=identity.pet_id,
            user_id="ou_user",
            source="feishu",
            event_type="feishu_message_received",
            source_ref=PetSourceRef(
                platform="lark",
                conversation_id="oc_1",
                message_id="om_1",
            ),
            payload={"text_len": 12},
            created_at="2026-06-15T00:00:00+00:00",
        )
    )

    events = store.list_events_after(identity.pet_id, after_id=0)
    state = store.get_pet_state(identity.pet_id)

    assert stored.id == 1
    assert len(events) == 1
    assert events[0].source_ref.message_id == "om_1"
    assert events[0].state_after is not None
    assert state is not None
    assert state.state == "waiting"
    assert state.last_event_id == 1


def test_store_lists_only_events_after_cursor(tmp_path: Path) -> None:
    store = PetLiveStore(tmp_path / "pet_live.db")
    identity = get_or_create_identity(store, feishu_open_id="ou_user")

    for event_type in ["feishu_message_received", "router_decision_made"]:
        store.append_event(
            PetEvent(
                pet_id=identity.pet_id,
                user_id="ou_user",
                source="test",
                event_type=event_type,
                source_ref=PetSourceRef(platform="lark"),
                payload={},
                created_at="2026-06-15T00:00:00+00:00",
            )
        )

    events = store.list_events_after(identity.pet_id, after_id=1)

    assert [event.event_type for event in events] == ["router_decision_made"]
