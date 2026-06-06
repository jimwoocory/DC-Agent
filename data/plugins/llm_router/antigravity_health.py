"""Circuit-breaker health state for the Antigravity CLI route."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

_STATE_PATH = Path(
    os.environ.get(
        "DC_ANTIGRAVITY_HEALTH_PATH",
        "/Users/dianchi/DC-Agent/data/antigravity_health.json",
    )
)
_HISTORY_PATH = Path(
    os.environ.get(
        "DC_ANTIGRAVITY_HEALTH_HISTORY_PATH",
        "/Users/dianchi/DC-Agent/data/antigravity_health_events.jsonl",
    )
)
_HISTORY_LIMIT = 200

_TERMINAL_CODES = {
    "auth_required",
    "bad_cli_args",
    "bin_not_allowed",
}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _load_state() -> dict[str, Any]:
    try:
        data = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def _save_state(state: dict[str, Any]) -> None:
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _STATE_PATH.with_suffix(_STATE_PATH.suffix + ".tmp")
        tmp.write_text(
            json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        tmp.replace(_STATE_PATH)
    except Exception:
        # Health state must never break the chat path.
        pass


def _append_history_event(event_type: str, state: dict[str, Any]) -> None:
    try:
        _HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        event = {
            "ts": time.time(),
            "event": event_type,
            "status": state.get("status", ""),
            "available": bool(state.get("available", False)),
            "reason": state.get("reason", ""),
            "error_code": state.get("last_error_code", ""),
            "error": state.get("last_error", ""),
            "consecutive_failures": int(state.get("consecutive_failures") or 0),
            "remaining_seconds": int(state.get("remaining_seconds") or 0),
        }
        if event_type == "success":
            event["elapsed_sec"] = round(
                float(state.get("last_success_elapsed_sec") or 0.0), 3
            )
        with _HISTORY_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception:
        # Observability must never break the chat path.
        pass


def _read_history(limit: int = _HISTORY_LIMIT) -> list[dict[str, Any]]:
    try:
        lines = _HISTORY_PATH.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    except Exception:
        return []

    events: list[dict[str, Any]] = []
    for line in lines[-max(1, limit) :]:
        try:
            item = json.loads(line)
        except Exception:
            continue
        if isinstance(item, dict):
            events.append(item)
    return events


def get_antigravity_health() -> dict[str, Any]:
    state = _load_state()
    now = time.time()
    disabled_until = float(state.get("disabled_until") or 0)
    if disabled_until > now:
        state["available"] = False
        state["remaining_seconds"] = int(disabled_until - now)
    else:
        state["available"] = True
        state["remaining_seconds"] = 0
    return state


def antigravity_allowed() -> tuple[bool, str, dict[str, Any]]:
    state = get_antigravity_health()
    if state.get("available") is False:
        reason = str(state.get("reason") or state.get("last_error_code") or "unknown")
        return False, reason, state
    return True, "", state


def mark_antigravity_success(*, elapsed_sec: float = 0.0) -> dict[str, Any]:
    now = time.time()
    state = _load_state()
    state.update(
        {
            "status": "healthy",
            "available": True,
            "disabled_until": 0,
            "remaining_seconds": 0,
            "consecutive_failures": 0,
            "last_success_at": now,
            "last_success_elapsed_sec": round(float(elapsed_sec or 0.0), 3),
            "last_error_code": "",
            "last_error": "",
            "reason": "",
        }
    )
    _save_state(state)
    _append_history_event("success", state)
    return state


def mark_antigravity_failure(
    *,
    error_code: str | None,
    error: str | None = None,
) -> dict[str, Any]:
    now = time.time()
    state = _load_state()
    code = (error_code or "unknown").strip() or "unknown"
    consecutive_failures = int(state.get("consecutive_failures") or 0) + 1

    auth_cooldown = _env_int("DC_ANTIGRAVITY_AUTH_COOLDOWN_SECONDS", 30 * 60)
    transient_cooldown = _env_int("DC_ANTIGRAVITY_TRANSIENT_COOLDOWN_SECONDS", 5 * 60)
    transient_threshold = max(
        1, _env_int("DC_ANTIGRAVITY_TRANSIENT_FAILURE_THRESHOLD", 2)
    )

    disabled_until = 0.0
    status = "degraded"
    reason = code
    if code in _TERMINAL_CODES:
        disabled_until = now + auth_cooldown
        status = "open"
    elif consecutive_failures >= transient_threshold:
        disabled_until = now + transient_cooldown
        status = "open"

    state.update(
        {
            "status": status,
            "available": disabled_until <= now,
            "disabled_until": disabled_until,
            "remaining_seconds": max(0, int(disabled_until - now)),
            "consecutive_failures": consecutive_failures,
            "last_failure_at": now,
            "last_error_code": code,
            "last_error": (error or "")[:500],
            "reason": reason,
        }
    )
    _save_state(state)
    _append_history_event("failure", state)
    return state


def record_antigravity_circuit_fallback(
    *, reason: str | None = None, state: dict[str, Any] | None = None
) -> None:
    snapshot = dict(state or get_antigravity_health())
    if reason:
        snapshot["reason"] = reason
    _append_history_event("circuit_fallback", snapshot)


def summarize_antigravity_history(limit: int = _HISTORY_LIMIT) -> dict[str, Any]:
    events = _read_history(limit=limit)
    event_counts: dict[str, int] = {}
    reason_counts: dict[str, int] = {}
    for event in events:
        event_name = str(event.get("event") or "unknown")
        reason = str(event.get("reason") or event.get("error_code") or "")
        event_counts[event_name] = event_counts.get(event_name, 0) + 1
        if reason:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
    return {
        "history_path": str(_HISTORY_PATH),
        "event_count": len(events),
        "event_counts": event_counts,
        "reason_counts": reason_counts,
        "recent_events": events[-10:],
    }
