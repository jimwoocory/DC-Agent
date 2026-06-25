"""Pet Live APIs for the desktop shell."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dc_engines.pet_live.codex_pets_adapter import codex_pets_payload
from dc_engines.pet_live.contracts import PetIdentity, PetState
from dc_engines.pet_live.event_bus import publish_pet_event
from dc_engines.pet_live.identity import get_or_create_identity
from dc_engines.pet_live.integrations import pet_live_enabled
from dc_engines.pet_live.store import PetLiveStore

from astrbot.dashboard.asgi_runtime import request, send_file

from .route import Response, Route, RouteContext

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SESSION_COOKIE = "dc_feishu_session"


class PetLiveRoute(Route):
    def __init__(self, context: RouteContext, dc_root: Path | None = None) -> None:
        super().__init__(context)
        self.dc_root = dc_root or PROJECT_ROOT
        self.routes = {
            "/pet/bind-desktop": ("POST", self.pet_bind_desktop),
            "/pet/me": ("GET", self.pet_me),
            "/pet/events": ("GET", self.pet_events),
            "/pet/heartbeat": ("POST", self.pet_heartbeat),
            "/pet/actions": ("POST", self.pet_actions),
            "/pet/assets/<asset_id>": ("GET", self.pet_asset),
            "/pet/assets/<asset_id>/<filename>": ("GET", self.pet_asset_file),
        }
        self.register_routes()

    async def pet_bind_desktop(self):
        disabled = self._disabled_response()
        if disabled is not None:
            return disabled
        session = self._desktop_session_cookie()
        if not session:
            return Response().error(f"{SESSION_COOKIE} cookie is required").__dict__

        data = await request.get_json(silent=True) or {}
        authorized = self._binding_authorized()
        session_identity = _identity_from_signed_session(session)
        provided_feishu_open_id = str(data.get("feishu_open_id") or "").strip()
        provided_employee_id = str(data.get("employee_id") or "").strip()
        if authorized:
            feishu_open_id = provided_feishu_open_id
            employee_id = provided_employee_id
            if session_identity is not None:
                feishu_open_id = feishu_open_id or session_identity["feishu_open_id"]
                employee_id = employee_id or session_identity["employee_id"]
        else:
            if session_identity is None:
                if not provided_feishu_open_id and not provided_employee_id:
                    return (
                        Response()
                        .error("pet live session is expired or invalid")
                        .__dict__
                    )
                return Response().error("pet live binding is not authorized").__dict__
            feishu_open_id = session_identity["feishu_open_id"]
            employee_id = session_identity["employee_id"]
        if not employee_id and not feishu_open_id:
            return (
                Response().error("employee_id or feishu_open_id is required").__dict__
            )

        store = PetLiveStore(self._store_path())
        existing_for_session = store.get_identity_by_desktop_session_id(session)
        existing_for_identity = (
            store.get_identity_by_employee_id(employee_id)
            if employee_id
            else store.get_identity_by_feishu_open_id(feishu_open_id)
        )
        if (
            existing_for_session is not None
            and existing_for_identity is not None
            and existing_for_session.pet_id != existing_for_identity.pet_id
        ):
            return Response().error("desktop session is already bound").__dict__
        if (
            existing_for_session is not None
            and existing_for_identity is None
            and (
                (employee_id and existing_for_session.employee_id != employee_id)
                or (
                    feishu_open_id
                    and existing_for_session.feishu_open_id != feishu_open_id
                )
            )
        ):
            return Response().error("desktop session is already bound").__dict__

        identity = get_or_create_identity(
            store,
            feishu_open_id=feishu_open_id,
            employee_id=employee_id,
            desktop_session_id=session,
        )
        event = publish_pet_event(
            store,
            pet_id=identity.pet_id,
            user_id=identity.employee_id or identity.feishu_open_id,
            source="desktop",
            event_type="desktop_session_bound",
            source_ref={
                "employee_id": identity.employee_id,
                "desktop_session_id": session,
            },
            payload={"employee_id": identity.employee_id},
        )
        state = store.get_pet_state(identity.pet_id)
        return (
            Response()
            .ok(
                {
                    "event_id": event.id,
                    "identity": _identity_to_dict(identity),
                    "pet": state.to_dict() if state else {},
                    "light_feedback": _light_feedback(state),
                    "codex_pets": codex_pets_payload(state) if state else {},
                }
            )
            .__dict__
        )

    async def pet_me(self):
        disabled = self._disabled_response()
        if disabled is not None:
            return disabled
        resolved = self._resolve_identity()
        if resolved is None:
            store = PetLiveStore(self._store_path())
            return self._unbound_dashboard_response(store)
        if isinstance(resolved, dict):
            return resolved
        store, identity = resolved
        state = store.get_pet_state(identity.pet_id)
        if state is None:
            return Response().error("pet state not found").__dict__
        return (
            Response()
            .ok(
                {
                    "pet": state.to_dict(),
                    "identity": _identity_to_dict(identity),
                    "last_event_id": state.last_event_id,
                    "light_feedback": _light_feedback(state),
                    "codex_pets": codex_pets_payload(state),
                }
            )
            .__dict__
        )

    async def pet_events(self):
        disabled = self._disabled_response()
        if disabled is not None:
            return disabled
        window = _event_window_from_request()
        after_id = _positive_int(request.args.get("after_id"), default=0)
        limit = _positive_int(request.args.get("limit"), default=100, max_value=300)
        resolved = self._resolve_identity()
        if resolved is None:
            store = PetLiveStore(self._store_path())
            events = store.list_all_events_after(
                after_id=after_id,
                limit=limit,
                created_from=window["from"],
                created_before=window["to"],
            )
            return (
                Response()
                .ok(
                    {
                        "events": [_event_to_dict(event) for event in events],
                        "last_event_id": events[-1].id if events else after_id,
                        "window": window,
                    }
                )
                .__dict__
            )
        if isinstance(resolved, dict):
            return resolved
        store, identity = resolved
        events = store.list_events_after(
            identity.pet_id,
            after_id=after_id,
            limit=limit,
            created_from=window["from"],
            created_before=window["to"],
        )
        return (
            Response()
            .ok(
                {
                    "events": [_event_to_dict(event) for event in events],
                    "last_event_id": events[-1].id if events else after_id,
                    "window": window,
                }
            )
            .__dict__
        )

    async def pet_heartbeat(self):
        disabled = self._disabled_response()
        if disabled is not None:
            return disabled
        resolved = self._resolve_identity()
        if resolved is None:
            return Response().error(f"{SESSION_COOKIE} cookie is required").__dict__
        if isinstance(resolved, dict):
            return resolved
        store, identity = resolved
        data = await request.get_json(silent=True) or {}
        event = publish_pet_event(
            store,
            pet_id=identity.pet_id,
            user_id=identity.employee_id or identity.feishu_open_id,
            source="desktop",
            event_type="desktop_heartbeat",
            source_ref={
                "employee_id": identity.employee_id,
                "desktop_session_id": identity.desktop_session_id,
            },
            payload={
                "last_event_id": int(data.get("last_event_id") or 0),
                "app_version": str(data.get("app_version") or ""),
            },
        )
        return Response().ok({"event_id": event.id}).__dict__

    async def pet_actions(self):
        disabled = self._disabled_response()
        if disabled is not None:
            return disabled
        resolved = self._resolve_identity()
        if resolved is None:
            return Response().error(f"{SESSION_COOKIE} cookie is required").__dict__
        if isinstance(resolved, dict):
            return resolved
        store, identity = resolved
        data = await request.get_json(silent=True) or {}
        action = str(data.get("action") or "").strip()
        if not action:
            return Response().error("action is required").__dict__
        event_type = "pet_fed" if action == "feed" else "pet_card_action"
        event = publish_pet_event(
            store,
            pet_id=identity.pet_id,
            user_id=identity.employee_id or identity.feishu_open_id,
            source="desktop",
            event_type=event_type,
            source_ref={
                "employee_id": identity.employee_id,
                "desktop_session_id": identity.desktop_session_id,
            },
            payload={"action": action, "payload": dict(data.get("payload") or {})},
        )
        state = store.get_pet_state(identity.pet_id)
        return (
            Response()
            .ok({"event_id": event.id, "pet": state.to_dict() if state else {}})
            .__dict__
        )

    async def pet_asset(self, asset_id: str):
        disabled = self._disabled_response()
        if disabled is not None:
            return disabled
        safe_asset_id = _safe_asset_id(asset_id)
        if not safe_asset_id:
            return Response().error("invalid asset_id").__dict__
        manifest_path = self._asset_manifest_path(safe_asset_id)
        if not manifest_path.exists():
            return Response().error("pet asset not found").__dict__
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return Response().error("invalid pet asset manifest").__dict__
        if not isinstance(manifest, dict):
            return Response().error("invalid pet asset manifest").__dict__
        return (
            Response()
            .ok(
                {
                    "asset_id": safe_asset_id,
                    "manifest": manifest,
                    "base_path": f"/api/pet/assets/{safe_asset_id}",
                }
            )
            .__dict__
        )

    async def pet_asset_file(self, asset_id: str, filename: str):
        disabled = self._disabled_response()
        if disabled is not None:
            return disabled
        safe_asset_id = _safe_asset_id(asset_id)
        safe_filename = _safe_asset_filename(filename)
        if not safe_asset_id or not safe_filename:
            return Response().error("invalid asset file").__dict__
        path = self.dc_root / "data" / "pet_assets" / safe_asset_id / safe_filename
        if not path.exists() or not path.is_file():
            return Response().error("pet asset file not found").__dict__
        return await send_file(path)

    def _resolve_identity(self) -> tuple[PetLiveStore, Any] | dict | None:
        session = self._desktop_session_cookie()
        if not session:
            return None
        store = PetLiveStore(self._store_path())
        identity = store.get_identity_by_desktop_session_id(session)
        if identity is None:
            return Response().error("desktop session is not bound to a pet").__dict__
        return store, identity

    def _store_path(self) -> Path:
        return self.dc_root / "data" / "feishu_pet.db"

    def _asset_manifest_path(self, asset_id: str) -> Path:
        return self.dc_root / "data" / "pet_assets" / asset_id / "pet.json"

    def _disabled_response(self) -> dict | None:
        if not pet_live_enabled():
            return Response().error("pet live disabled").__dict__
        if os.environ.get("PET_LIVE_API_ENABLED", "1").strip().lower() in {
            "0",
            "false",
            "no",
            "off",
        }:
            return Response().error("pet live api disabled").__dict__
        return None

    def _desktop_session_cookie(self) -> str:
        return (request.cookies.get(SESSION_COOKIE) or "").strip()

    def _unbound_dashboard_response(self, store: PetLiveStore) -> dict:
        state = store.get_latest_pet_state() or PetState(
            pet_id="system",
            user_id="",
            name="小助手",
            species="assistant",
            asset_id="",
            state="idle",
            emotion="unknown",
            scene="unknown",
            level=0,
            energy=0,
        )
        return (
            Response()
            .ok(
                {
                    "pet": state.to_dict(),
                    "identity": None,
                    "last_event_id": 0,
                    "light_feedback": _light_feedback(state),
                    "codex_pets": codex_pets_payload(state),
                }
            )
            .__dict__
        )

    def _binding_authorized(self) -> bool:
        expected = os.environ.get("PET_LIVE_BINDING_TOKEN", "").strip()
        if not expected:
            return False
        actual = request.headers.get("X-Pet-Live-Bind-Token", "").strip()
        authorization = request.headers.get("Authorization", "").strip()
        if authorization.lower().startswith("bearer "):
            actual = authorization[7:].strip()
        return actual == expected


def _positive_int(
    value: str | None,
    *,
    default: int,
    max_value: int | None = None,
) -> int:
    try:
        parsed = int(value) if value is not None else default
    except (TypeError, ValueError):
        parsed = default
    parsed = max(0, parsed)
    if max_value is not None:
        parsed = min(parsed, max_value)
    return parsed


def _session_secret() -> bytes:
    raw = (
        os.environ.get("SITE_SESSION_SECRET", "").strip()
        or os.environ.get("FEISHU_SESSION_SECRET", "").strip()
        or "dc-agent-local-dev-secret-change-me"
    )
    return raw.encode("utf-8")


def _unb64(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode((data + padding).encode("ascii"))


def _verify_signed_session(value: str) -> dict[str, Any] | None:
    try:
        body, sig = value.split(".", 1)
        expected = hmac.new(
            _session_secret(), body.encode("ascii"), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(_unb64(sig), expected):
            return None
        payload = json.loads(_unb64(body).decode("utf-8"))
    except (binascii.Error, ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    exp = int(payload.get("exp") or 0)
    if exp and exp < time.time():
        return None
    return payload


def _identity_from_signed_session(value: str) -> dict[str, str] | None:
    payload = _verify_signed_session(value)
    user = payload.get("user") if isinstance(payload, dict) else None
    if not isinstance(user, dict):
        return None
    feishu_open_id = str(user.get("open_id") or "").strip()
    employee_id = (
        str(user.get("user_id") or "").strip()
        or str(user.get("union_id") or "").strip()
        or str(user.get("email") or "").strip()
        or feishu_open_id
    )
    if not employee_id and not feishu_open_id:
        return None
    return {"employee_id": employee_id, "feishu_open_id": feishu_open_id}


def _event_window_from_request() -> dict[str, str]:
    return {
        "from": _iso_datetime_param(request.args.get("from")),
        "to": _iso_datetime_param(request.args.get("to")),
    }


def _iso_datetime_param(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _safe_asset_id(asset_id: str) -> str:
    cleaned = str(asset_id or "").strip()
    if not cleaned:
        return ""
    if "/" in cleaned or "\\" in cleaned or ".." in cleaned:
        return ""
    return cleaned


def _safe_asset_filename(filename: str) -> str:
    cleaned = str(filename or "").strip()
    if not cleaned:
        return ""
    if "/" in cleaned or "\\" in cleaned or ".." in cleaned:
        return ""
    allowed_suffixes = {".webp", ".png", ".jpg", ".jpeg", ".json"}
    if Path(cleaned).suffix.lower() not in allowed_suffixes:
        return ""
    return cleaned


def _event_to_dict(event: Any) -> dict[str, Any]:
    return {
        "id": event.id,
        "pet_id": event.pet_id,
        "user_id": event.user_id,
        "source": event.source,
        "event_type": event.event_type,
        "source_ref": event.source_ref.to_dict(),
        "payload": event.payload,
        "state_after": event.state_after.to_dict() if event.state_after else {},
        "created_at": event.created_at,
    }


def _identity_to_dict(identity: PetIdentity) -> dict[str, str]:
    return {
        "pet_id": identity.pet_id,
        "employee_id": identity.employee_id,
        "feishu_open_id": identity.feishu_open_id,
        "desktop_session_id": identity.desktop_session_id,
    }


def _light_feedback(state: PetState | None) -> dict[str, Any]:
    if state is None:
        return {}
    signal = state.last_signal if isinstance(state.last_signal, dict) else {}
    return {
        "mood": state.mood,
        "focus_level": state.focus_level,
        "memory_affinity": state.memory_affinity,
        "work_rhythm": state.work_rhythm,
        "last_signal": signal,
    }
