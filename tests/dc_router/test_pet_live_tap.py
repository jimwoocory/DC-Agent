from pathlib import Path
from unittest.mock import MagicMock

from data.plugins.dc_router.pet_live_tap import record_router_pet_event
from dc_engines.pet_live.store import PetLiveStore


def _event(sender_id: str = "ou_user", employee_id: str = "emp_001") -> MagicMock:
    event = MagicMock()
    event.get_sender_id.return_value = sender_id
    event.get_platform_name.return_value = "lark"
    event.get_platform_id.return_value = "lark_main"
    event.get_extra.side_effect = lambda key: {"employee_id": employee_id}.get(key, "")
    event.session_id = "oc_1"
    event.message_id = "om_1"
    event.unified_msg_origin = "lark:oc_1:ou_user"
    return event


def test_record_router_pet_event_writes_traceable_event(tmp_path: Path) -> None:
    db_path = tmp_path / "pet_live.db"

    stored = record_router_pet_event(
        _event(),
        event_type="router_decision_made",
        payload={"intent": "hermes_escalation", "provider": "cli/codex"},
        db_path=db_path,
        router_trace_id="trace_1",
    )

    assert stored is not None
    assert stored.source == "router"
    assert stored.user_id == "emp_001"
    assert stored.source_ref.employee_id == "emp_001"
    assert stored.source_ref.router_trace_id == "trace_1"
    state = PetLiveStore(db_path).get_pet_state(stored.pet_id)
    assert state is not None
    assert state.state == "thinking"


def test_record_router_pet_event_is_noop_without_sender(tmp_path: Path) -> None:
    stored = record_router_pet_event(
        _event(sender_id="", employee_id=""),
        event_type="router_decision_made",
        db_path=tmp_path / "pet_live.db",
    )

    assert stored is None


def test_record_router_pet_event_respects_global_disable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("PET_LIVE_ENABLED", "false")

    stored = record_router_pet_event(
        _event(),
        event_type="router_decision_made",
        db_path=tmp_path / "pet_live.db",
    )

    assert stored is None
