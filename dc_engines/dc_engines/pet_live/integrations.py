"""Small helpers for future Router/Harness/Hermes taps."""

from __future__ import annotations

import os
from typing import Any

from .contracts import PetSourceRef


def pet_live_enabled() -> bool:
    """Global kill switch shared by Pet Live integration entry points."""

    return os.environ.get("PET_LIVE_ENABLED", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def source_ref_from_event(event: Any) -> PetSourceRef:
    return PetSourceRef(
        employee_id=employee_id_from_source(event),
        platform=str(getattr(event, "get_platform_name", lambda: "")() or ""),
        conversation_id=str(getattr(event, "session_id", "") or ""),
        message_id=str(getattr(event, "message_id", "") or ""),
    )


def employee_id_from_source(source: Any) -> str:
    """Best-effort employee_id resolver for events and task payloads."""

    if isinstance(source, dict):
        return _first_nonempty(
            source.get("employee_id"),
            source.get("employeeId"),
            source.get("staff_id"),
            source.get("user", {}).get("employee_id")
            if isinstance(source.get("user"), dict)
            else "",
        )
    getter = getattr(source, "get_extra", None)
    if callable(getter):
        for key in ("employee_id", "employeeId", "staff_id"):
            try:
                value = getter(key)
            except Exception:  # noqa: BLE001
                value = ""
            if value:
                return str(value).strip()
    payload = getattr(source, "payload", None)
    if isinstance(payload, dict):
        found = employee_id_from_source(payload)
        if found:
            return found
    for attr in ("employee_id", "employeeId", "staff_id"):
        value = getattr(source, attr, "")
        if isinstance(value, (str, int)) and value:
            return str(value).strip()
    return ""


def _first_nonempty(*values: object) -> str:
    for value in values:
        if not isinstance(value, (str, int)):
            continue
        text = str(value or "").strip()
        if text:
            return text
    return ""
