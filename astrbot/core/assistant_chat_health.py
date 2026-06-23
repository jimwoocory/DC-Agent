from __future__ import annotations

import time
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any


def epoch_to_utc_iso(value: float) -> str:
    return (
        datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")
    )


class AssistantChatHealthTracker:
    def __init__(self, *, max_events: int = 1000) -> None:
        self.activity_events: deque[dict[str, Any]] = deque(maxlen=max_events)
        self.running_runs: dict[str, dict[str, Any]] = {}

    def clear(self) -> None:
        self.activity_events.clear()
        self.running_runs.clear()

    def begin_run(
        self,
        *,
        kind: str,
        at: float | None = None,
    ) -> str:
        started_at = at if at is not None else time.time()
        run_id = str(uuid.uuid4())
        self.running_runs[run_id] = {
            "kind": self._normalize_kind(kind),
            "started_at": started_at,
            "last_seen_at": started_at,
        }
        self.record_activity(kind=kind, phase="started", at=started_at)
        return run_id

    def finish_run(
        self,
        run_id: str | None,
        *,
        phase: str,
        at: float | None = None,
    ) -> None:
        if not run_id:
            return
        event_at = at if at is not None else time.time()
        run = self.running_runs.pop(run_id, None)
        if not run:
            return
        self.record_activity(
            kind=str(run.get("kind") or "session"),
            phase=phase,
            at=event_at,
            started_at=float(run.get("started_at") or event_at),
        )

    def record_activity(
        self,
        *,
        kind: str,
        phase: str,
        at: float | None = None,
        started_at: float | None = None,
    ) -> None:
        event_at = at if at is not None else time.time()
        event: dict[str, Any] = {
            "ts": event_at,
            "kind": self._normalize_kind(kind),
            "phase": phase,
        }
        if started_at is not None:
            event["duration_sec"] = int(max(0, event_at - started_at))
        self.activity_events.append(event)

    def runtime_summary(self, *, now: float, stale_after_sec: int) -> dict[str, Any]:
        by_kind: dict[str, int] = {}
        stale_runs = 0
        oldest_started_at = self.oldest_started_at()

        for value in self.running_runs.values():
            kind = str(value.get("kind") or "session")
            by_kind[kind] = by_kind.get(kind, 0) + 1
            started_at = value.get("started_at")
            if (
                isinstance(started_at, (int, float))
                and now - started_at > stale_after_sec
            ):
                stale_runs += 1

        oldest_age = (
            int(max(0, now - oldest_started_at)) if oldest_started_at is not None else 0
        )
        return {
            "active_runs": len(self.running_runs),
            "active_conversations": len(self.running_runs),
            "active_sessions": by_kind.get("session", 0),
            "active_threads": by_kind.get("thread", 0),
            "active_platforms": by_kind.get("platform", 0),
            "stale_runs": stale_runs,
            "stale_after_sec": stale_after_sec,
            "oldest_run_age_sec": oldest_age,
            "oldest_run_started_at": (
                epoch_to_utc_iso(oldest_started_at)
                if oldest_started_at is not None
                else None
            ),
        }

    def activity_summary(self, *, now: float) -> dict[str, Any]:
        events = list(self.activity_events)
        start_events = [event for event in events if event.get("phase") == "started"]
        finish_events = [
            event
            for event in events
            if event.get("phase") in {"completed", "disconnected", "failed"}
        ]

        def count_recent(source: list[dict[str, Any]], seconds: int) -> int:
            return sum(
                1 for event in source if now - float(event.get("ts") or 0) <= seconds
            )

        def last_event(source: list[dict[str, Any]]) -> dict[str, Any] | None:
            return max(
                source, key=lambda event: float(event.get("ts") or 0), default=None
            )

        latest = last_event(events)
        latest_start = last_event(start_events)
        latest_finish = last_event(finish_events)
        recent_15m = [
            event for event in finish_events if now - float(event.get("ts") or 0) <= 900
        ]
        return {
            "started_5m": count_recent(start_events, 300),
            "started_15m": count_recent(start_events, 900),
            "started_60m": count_recent(start_events, 3600),
            "completed_15m": sum(
                1 for event in recent_15m if event.get("phase") == "completed"
            ),
            "failed_15m": sum(
                1 for event in recent_15m if event.get("phase") == "failed"
            ),
            "disconnected_15m": sum(
                1 for event in recent_15m if event.get("phase") == "disconnected"
            ),
            "last_event": str(latest.get("phase")) if latest else None,
            "last_kind": str(latest.get("kind")) if latest else None,
            "last_event_at": epoch_to_utc_iso(float(latest["ts"])) if latest else None,
            "last_started_at": (
                epoch_to_utc_iso(float(latest_start["ts"])) if latest_start else None
            ),
            "last_finished_at": (
                epoch_to_utc_iso(float(latest_finish["ts"])) if latest_finish else None
            ),
            "last_duration_sec": int(latest_finish.get("duration_sec") or 0)
            if latest_finish
            else 0,
        }

    def oldest_started_at(self) -> float | None:
        started_values = [
            float(value["started_at"])
            for value in self.running_runs.values()
            if isinstance(value.get("started_at"), (int, float))
        ]
        return min(started_values, default=None)

    @staticmethod
    def _normalize_kind(kind: str) -> str:
        if kind in {"platform", "thread"}:
            return kind
        return "session"


assistant_chat_health_tracker = AssistantChatHealthTracker()
