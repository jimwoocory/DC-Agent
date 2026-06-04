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
    return state
