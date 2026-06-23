"""Best-effort Pet Live event tap for dc_router.

This module deliberately keeps Router decoupled from Pet Live Core: failures are
swallowed and callers should never branch on the result for routing behavior.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from dc_engines.pet_live.contracts import StoredPetEvent
from dc_engines.pet_live.event_bus import publish_pet_event
from dc_engines.pet_live.identity import get_or_create_identity
from dc_engines.pet_live.integrations import employee_id_from_source, pet_live_enabled
from dc_engines.pet_live.store import PetLiveStore

from astrbot.api import logger


def record_router_pet_event(
    event: Any,
    *,
    event_type: str,
    payload: dict[str, Any] | None = None,
    db_path: str | Path | None = None,
    router_trace_id: str = "",
) -> StoredPetEvent | None:
    if not _tap_enabled():
        return None
    user_id = _sender_id(event)
    employee_id = employee_id_from_source(event)
    if not user_id and not employee_id:
        return None

    try:
        store = PetLiveStore(db_path or _default_db_path())
        identity = get_or_create_identity(
            store,
            feishu_open_id=user_id,
            employee_id=employee_id,
        )
        event_user_id = identity.employee_id or identity.feishu_open_id
        return publish_pet_event(
            store,
            pet_id=identity.pet_id,
            user_id=event_user_id,
            source="router",
            event_type=event_type,
            source_ref={
                "employee_id": identity.employee_id,
                "platform": _platform(event),
                "conversation_id": _conversation_id(event),
                "message_id": _message_id(event),
                "router_trace_id": router_trace_id,
            },
            payload={"employee_id": identity.employee_id, **(payload or {})},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[dc_router] pet live tap failed event=%s err=%s", event_type, exc
        )
        return None


def _tap_enabled() -> bool:
    if not pet_live_enabled():
        return False
    return os.environ.get("PET_LIVE_TAP_ENABLED", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _default_db_path() -> Path:
    configured = os.environ.get("PET_LIVE_DB_PATH", "").strip()
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[2] / "feishu_pet.db"


def _sender_id(event: Any) -> str:
    getter = getattr(event, "get_sender_id", None)
    if not callable(getter):
        return ""
    try:
        return str(getter() or "")
    except Exception:  # noqa: BLE001
        return ""


def _platform(event: Any) -> str:
    getter = getattr(event, "get_platform_name", None)
    if callable(getter):
        try:
            platform = str(getter() or "")
            if platform:
                return platform
        except Exception:  # noqa: BLE001
            pass
    getter = getattr(event, "get_platform_id", None)
    if callable(getter):
        try:
            return str(getter() or "")
        except Exception:  # noqa: BLE001
            return ""
    return ""


def _conversation_id(event: Any) -> str:
    return str(
        getattr(event, "session_id", "")
        or getattr(event, "conversation_id", "")
        or getattr(event, "unified_msg_origin", "")
        or ""
    )


def _message_id(event: Any) -> str:
    return str(
        getattr(event, "message_id", "")
        or getattr(getattr(event, "message_obj", None), "message_id", "")
        or ""
    )
