from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

REPAIR_ENGINE = Path("scripts-watchdog/repair_engine.py")
REVIEW_NOTIFIER = Path("scripts-watchdog/review_notifier.py")
WATCHDOG_SCRIPT = Path("scripts-watchdog/dc-watchdog.sh")


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _write_review_state(path: Path, *, status: str = "pending") -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "incidents": {},
                "services": {},
                "reviews": {
                    "incident-1": {
                        "service": "assistant_chat_health",
                        "status": status,
                        "created_at_unix": 1_000,
                        "updated_at_unix": 1_000,
                        "summary": "Business health needs investigation.",
                        "reason": "No fixed automatic action is authorized.",
                        "risk": "high",
                        "action_id": "manual_intervention",
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def test_review_notification_candidates_choose_highest_due_stage(tmp_path) -> None:
    engine = _load_module(REPAIR_ENGINE, "review_notification_candidates")
    state = tmp_path / "repair_state.json"
    _write_review_state(state)

    created = engine.collect_due_review_notifications(state_path=state, now=1_001)
    assert [item["stage"] for item in created] == ["created"]

    overdue = engine.collect_due_review_notifications(state_path=state, now=4_700)
    assert [item["stage"] for item in overdue] == ["pending_60m"]
    assert overdue[0]["level"] == "critical"
    assert "command" not in json.dumps(overdue)


def test_review_notifier_marks_only_successful_delivery_and_deduplicates(
    tmp_path,
) -> None:
    engine = _load_module(REPAIR_ENGINE, "review_notification_engine")
    notifier = _load_module(REVIEW_NOTIFIER, "review_notifier")
    state = tmp_path / "repair_state.json"
    events = tmp_path / "review_notifications.jsonl"
    _write_review_state(state)
    sends: list[dict] = []

    def sender(**kwargs):
        sends.append(kwargs)
        return SimpleNamespace(success=True, errors={})

    result = notifier.run_notifications(
        state_path=state,
        events_path=events,
        dashboard_url="http://127.0.0.1:6185/",
        sender=sender,
        engine=engine,
        now=1_001,
    )
    repeated = notifier.run_notifications(
        state_path=state,
        events_path=events,
        dashboard_url="http://127.0.0.1:6185/",
        sender=sender,
        engine=engine,
        now=1_002,
    )

    assert result == {"due": 1, "sent": 1, "failed": 0}
    assert repeated == {"due": 0, "sent": 0, "failed": 0}
    assert sends[0]["action_label"] == "打开 Dashboard 审核"
    assert sends[0]["action_url"] == "http://127.0.0.1:6185/"
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["reviews"]["incident-1"]["notifications"]["created"] == 1_001
    snapshot = engine.collect_repair_status(
        state_path=state,
        events_path=tmp_path / "repairs.jsonl",
        now=1_002,
    )
    assert snapshot["reviews"][0]["notification_stages"] == ["created"]
    assert len(events.read_text(encoding="utf-8").splitlines()) == 1


def test_review_notifier_does_not_mark_failed_delivery(tmp_path) -> None:
    engine = _load_module(REPAIR_ENGINE, "review_notification_failure_engine")
    notifier = _load_module(REVIEW_NOTIFIER, "review_notifier_failure")
    state = tmp_path / "repair_state.json"
    events = tmp_path / "review_notifications.jsonl"
    _write_review_state(state)

    result = notifier.run_notifications(
        state_path=state,
        events_path=events,
        dashboard_url="http://127.0.0.1:6185/",
        sender=lambda **_kwargs: SimpleNamespace(
            success=False,
            errors={"lark": "offline"},
        ),
        engine=engine,
        now=1_001,
    )

    assert result == {"due": 1, "sent": 0, "failed": 1}
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert "notifications" not in saved["reviews"]["incident-1"]
    assert not events.exists()


def test_watchdog_schedules_review_notifier_without_blocking() -> None:
    source = WATCHDOG_SCRIPT.read_text(encoding="utf-8")

    assert "review_notifier.py" in source
    assert "nohup" in source
