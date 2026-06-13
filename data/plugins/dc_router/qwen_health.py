"""Circuit-breaker health state for the AIHubMix Qwen 3.6 Flash route.

Mirror of ``antigravity_health`` but for the Qwen 3.6 Flash provider that powers
casual chat.  When the model keeps returning empty responses we open the
breaker and dc-router falls back to ``aihubmix/gemini-3.5-flash`` until the
breaker auto-closes again (or an operator clears it via the dashboard).

Defaults are intentionally tighter than antigravity because qwen-flash has
historically returned empty without throwing an explicit error — we want a
single empty result to already start the cooldown, and two empties in a row to
fully open the breaker.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

try:
    from .paths import data_path
except ImportError:  # pragma: no cover - direct file-load compatibility
    from data.plugins.dc_router.paths import data_path

_STATE_PATH = Path(
    os.environ.get(
        "DC_QWEN_HEALTH_PATH",
        str(data_path("qwen_health.json")),
    )
)
_HISTORY_PATH = Path(
    os.environ.get(
        "DC_QWEN_HEALTH_HISTORY_PATH",
        str(data_path("qwen_health_events.jsonl")),
    )
)
_HISTORY_LIMIT = 200

_PROVIDER_ID = "aihubmix/qwen3.6-flash"
_FALLBACK_PROVIDER_ID = "aihubmix/gemini-3.5-flash"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


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
            "provider_id": _PROVIDER_ID,
            "status": state.get("status", ""),
            "available": bool(state.get("available", False)),
            "reason": state.get("reason", ""),
            "error_code": state.get("last_error_code", ""),
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


def get_qwen_health() -> dict[str, Any]:
    """Return the current circuit-breaker state for qwen3.6-flash."""
    state = _load_state()
    now = time.time()
    disabled_until = float(state.get("disabled_until") or 0)
    if disabled_until > now:
        state["available"] = False
        state["remaining_seconds"] = int(disabled_until - now)
    else:
        state["available"] = True
        state["remaining_seconds"] = 0
    state["provider_id"] = _PROVIDER_ID
    state["fallback_provider_id"] = _FALLBACK_PROVIDER_ID
    # Operator kill-switch via env var (no code change required to demote).
    if _env_bool("DC_QWEN_DISABLED", False):
        state["available"] = False
        state["remaining_seconds"] = (
            int(float(state.get("disabled_until") or 0) - now) or 86400
        )
        state["reason"] = state.get("reason") or "env:DC_QWEN_DISABLED"
    return state


def qwen3_6_flash_allowed() -> tuple[bool, str, dict[str, Any]]:
    """Return (allowed, reason, state) for routing decisions."""
    state = get_qwen_health()
    if state.get("available") is False:
        reason = str(state.get("reason") or state.get("last_error_code") or "unknown")
        return False, reason, state
    return True, "", state


def mark_qwen3_6_flash_success(*, elapsed_sec: float = 0.0) -> dict[str, Any]:
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


def mark_qwen3_6_flash_failure(
    *,
    error_code: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """Open or extend the breaker on an empty/error response from qwen-flash.

    Default policy:
      * 1 empty result → 60s cooldown (so we don't keep hammering it within the
        same minute) but ``available`` stays True (it might be a transient
        single-shot issue).
      * 2 consecutive empty results → 5min full breaker open.
    """
    now = time.time()
    state = _load_state()
    code = (error_code or "empty").strip() or "empty"
    consecutive_failures = int(state.get("consecutive_failures") or 0) + 1

    soft_cooldown = _env_int("DC_QWEN_SOFT_COOLDOWN_SECONDS", 60)
    open_cooldown = _env_int("DC_QWEN_OPEN_COOLDOWN_SECONDS", 5 * 60)
    open_threshold = max(1, _env_int("DC_QWEN_FAILURE_THRESHOLD", 2))

    if consecutive_failures >= open_threshold:
        disabled_until = now + open_cooldown
        status = "open"
    else:
        disabled_until = now + soft_cooldown
        status = "degraded"

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
            "reason": code,
        }
    )
    _save_state(state)
    _append_history_event("failure", state)
    return state


def reset_qwen_health() -> dict[str, Any]:
    """Manually clear the breaker (used by dashboard/CLI)."""
    state: dict[str, Any] = {
        "status": "healthy",
        "available": True,
        "disabled_until": 0,
        "remaining_seconds": 0,
        "consecutive_failures": 0,
        "last_error_code": "",
        "last_error": "",
        "reason": "",
    }
    _save_state(state)
    _append_history_event("reset", state)
    return state


def summarize_qwen_history(limit: int = _HISTORY_LIMIT) -> dict[str, Any]:
    try:
        lines = _HISTORY_PATH.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return {
            "history_path": str(_HISTORY_PATH),
            "event_count": 0,
            "event_counts": {},
            "reason_counts": {},
            "recent_events": [],
        }
    except Exception:
        return {
            "history_path": str(_HISTORY_PATH),
            "event_count": 0,
            "event_counts": {},
            "reason_counts": {},
            "recent_events": [],
        }
    events: list[dict[str, Any]] = []
    event_counts: dict[str, int] = {}
    reason_counts: dict[str, int] = {}
    for line in lines[-max(1, limit) :]:
        try:
            item = json.loads(line)
        except Exception:
            continue
        if not isinstance(item, dict):
            continue
        events.append(item)
        event_name = str(item.get("event") or "unknown")
        event_counts[event_name] = event_counts.get(event_name, 0) + 1
        reason = str(item.get("reason") or item.get("error_code") or "")
        if reason:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
    return {
        "history_path": str(_HISTORY_PATH),
        "event_count": len(events),
        "event_counts": event_counts,
        "reason_counts": reason_counts,
        "recent_events": events[-10:],
    }
