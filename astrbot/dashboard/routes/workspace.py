"""Desktop workspace APIs backed by the Feishu desktop session."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from dc_engines.employee_insight_loop import TextSendResult
from dc_engines.pet_live.contracts import StoredPetEvent
from dc_engines.pet_live.event_bus import publish_pet_event
from dc_engines.pet_live.store import PetLiveStore
from quart import request

from .pet_live import PROJECT_ROOT, SESSION_COOKIE
from .route import Response, Route, RouteContext


class WorkspaceMessageSender(Protocol):
    async def send_text(
        self,
        receive_id: str,
        text: str,
        *,
        receive_id_type: str,
    ) -> TextSendResult: ...


@dataclass(slots=True)
class WorkspaceReadResult:
    success: bool
    messages: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""


class WorkspaceMessageReader(Protocol):
    async def list_messages(
        self,
        *,
        chat_id: str,
        conversation_id: str,
        identity: Any,
        limit: int,
    ) -> WorkspaceReadResult: ...


class FeishuWorkspaceMessageSender:
    async def send_text(
        self,
        receive_id: str,
        text: str,
        *,
        receive_id_type: str,
    ) -> TextSendResult:
        from dc_engines.feishu_hub import get_client, get_hub
        from lark_oapi.api.im.v1 import (
            CreateMessageRequest,
            CreateMessageRequestBody,
        )

        client = get_client()
        if client is None:
            return TextSendResult(
                success=False,
                error="Feishu credentials disabled",
                raw={"receive_id": receive_id, "receive_id_type": receive_id_type},
            )
        req = (
            CreateMessageRequest.builder()
            .receive_id_type(receive_id_type)
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(receive_id)
                .msg_type("text")
                .content(json.dumps({"text": text}, ensure_ascii=False))
                .build()
            )
            .build()
        )
        try:
            resp = await client.im.v1.message.acreate(req)
            get_hub().record_call("im.message.create")
        except Exception as exc:  # noqa: BLE001
            get_hub().record_call("im.message.create", error=exc)
            return TextSendResult(
                success=False,
                error=f"{type(exc).__name__}: {exc}",
                raw={"receive_id": receive_id, "receive_id_type": receive_id_type},
            )
        if not resp.success():
            code = getattr(resp, "code", "?")
            msg = getattr(resp, "msg", "?")
            return TextSendResult(
                success=False,
                error=f"Feishu API error code={code} msg={msg}",
                raw={
                    "receive_id": receive_id,
                    "receive_id_type": receive_id_type,
                    "code": code,
                    "msg": msg,
                },
            )
        message_id = getattr(resp.data, "message_id", "") if resp.data else ""
        chat_id = getattr(resp.data, "chat_id", "") if resp.data else ""
        return TextSendResult(
            success=True,
            provider_message_id=str(message_id or ""),
            raw={
                "receive_id": receive_id,
                "receive_id_type": receive_id_type,
                "chat_id": str(chat_id or ""),
            },
        )


class FeishuWorkspaceMessageReader:
    async def list_messages(
        self,
        *,
        chat_id: str,
        conversation_id: str,
        identity: Any,
        limit: int,
    ) -> WorkspaceReadResult:
        from dc_engines.feishu_hub import get_client, get_credentials, get_hub
        from lark_oapi.api.im.v1 import ListMessageRequest

        client = get_client()
        if client is None:
            return WorkspaceReadResult(
                success=False,
                error="Feishu credentials disabled",
            )
        if not chat_id:
            return WorkspaceReadResult(
                success=False,
                error="chat_id is required for Feishu history",
            )
        end_time = int(time.time())
        lookback_minutes = _workspace_read_lookback_minutes()
        start_time = end_time - lookback_minutes * 60
        req = (
            ListMessageRequest.builder()
            .container_id_type("chat")
            .container_id(chat_id)
            .start_time(str(start_time))
            .end_time(str(end_time))
            .sort_type("ByCreateTimeAsc")
            .page_size(max(1, min(limit, 50)))
            .build()
        )
        try:
            resp = await client.im.v1.message.alist(req)
            get_hub().record_call("im.message.list")
        except Exception as exc:  # noqa: BLE001
            get_hub().record_call("im.message.list", error=exc)
            return WorkspaceReadResult(
                success=False,
                error=f"{type(exc).__name__}: {exc}",
            )
        if not resp.success():
            code = getattr(resp, "code", "?")
            msg = getattr(resp, "msg", "?")
            return WorkspaceReadResult(
                success=False,
                error=f"Feishu API error code={code} msg={msg}",
            )
        credentials = get_credentials()
        app_id = credentials.app_id if credentials else ""
        data = getattr(resp, "data", None)
        items = list(getattr(data, "items", None) or []) if data else []
        messages = [
            _feishu_message_to_workspace_message(
                item,
                conversation_id=conversation_id,
                app_id=app_id,
            )
            for item in items
        ]
        return WorkspaceReadResult(
            success=True,
            messages=[message for message in messages if message is not None],
        )


class WorkspaceRoute(Route):
    def __init__(
        self,
        context: RouteContext,
        dc_root: Path | None = None,
        message_sender: WorkspaceMessageSender | None = None,
        message_reader: WorkspaceMessageReader | None = None,
    ) -> None:
        super().__init__(context)
        self.dc_root = dc_root or PROJECT_ROOT
        self._message_sender = message_sender
        self._message_reader = message_reader
        self.routes = {
            "/workspace/conversations": ("GET", self.workspace_conversations),
            "/workspace/messages": [
                ("GET", self.workspace_messages),
                ("POST", self.workspace_send_message),
            ],
        }
        self.register_routes()

    async def workspace_conversations(self):
        resolved = self._resolve_identity()
        if isinstance(resolved, dict):
            return resolved
        store, identity = resolved
        events = store.list_events_after(identity.pet_id, after_id=0, limit=300)
        conversations = _workspace_conversations(identity, events)
        return (
            Response()
            .ok(
                {
                    "user": _identity_user(identity),
                    "conversations": conversations,
                    "integration_status": _workspace_integration_status(),
                }
            )
            .__dict__
        )

    async def workspace_messages(self):
        resolved = self._resolve_identity()
        if isinstance(resolved, dict):
            return resolved
        store, identity = resolved
        conversation_id = (
            str(request.args.get("conversation_id") or "").strip()
            or identity.feishu_open_id
        )
        events = store.list_events_after(identity.pet_id, after_id=0, limit=300)
        outbox_messages = _workspace_messages_from_events(
            events,
            conversation_id=conversation_id,
        )
        read_result = await self._read_workspace_messages(
            identity=identity,
            conversation_id=conversation_id,
            events=events,
        )
        if read_result.success:
            _record_assistant_history_events(
                store,
                identity=identity,
                conversation_id=conversation_id,
                messages=read_result.messages,
                existing_events=events,
            )
        messages = _merge_workspace_messages(
            read_result.messages if read_result.success else [],
            outbox_messages,
        )
        return (
            Response()
            .ok(
                {
                    "user": _identity_user(identity),
                    "conversation_id": conversation_id,
                    "messages": messages,
                    "integration_status": _workspace_integration_status(),
                    "history_status": (
                        "ok"
                        if read_result.success
                        else read_result.error
                        or "feishu_workspace_reader_not_configured"
                    ),
                }
            )
            .__dict__
        )

    async def workspace_send_message(self):
        resolved = self._resolve_identity()
        if isinstance(resolved, dict):
            return resolved
        store, identity = resolved
        data = await request.get_json(silent=True) or {}
        conversation_id = str(data.get("conversation_id") or "").strip()
        content = str(data.get("content") or "").strip()
        if not conversation_id:
            return Response().error("conversation_id is required").__dict__
        if not content:
            return Response().error("content is required").__dict__
        observed_event = publish_pet_event(
            store,
            pet_id=identity.pet_id,
            user_id=identity.employee_id or identity.feishu_open_id,
            source="feishu_workspace",
            event_type="feishu_message_received",
            source_ref={
                "employee_id": identity.employee_id,
                "platform": "desktop_workspace",
                "conversation_id": conversation_id,
                "desktop_session_id": identity.desktop_session_id,
            },
            payload={
                "employee_id": identity.employee_id,
                "direction": "desktop_to_assistant",
                "content": content,
                "text_len": len(content),
            },
        )
        send_result = await self._send_workspace_message(
            identity=identity,
            conversation_id=conversation_id,
            content=content,
        )
        delivered_event_id = 0
        if send_result.success:
            delivered = publish_pet_event(
                store,
                pet_id=identity.pet_id,
                user_id=identity.employee_id or identity.feishu_open_id,
                source="feishu_workspace",
                event_type="assistant_response_sent",
                source_ref={
                    "employee_id": identity.employee_id,
                    "platform": "desktop_workspace",
                    "conversation_id": conversation_id,
                    "message_id": send_result.provider_message_id,
                    "desktop_session_id": identity.desktop_session_id,
                },
                payload={
                    "employee_id": identity.employee_id,
                    "direction": "desktop_workspace_delivered",
                    "content": "已发送到飞书小助手",
                    "provider_message_id": send_result.provider_message_id,
                    "receive_id": str(send_result.raw.get("receive_id") or ""),
                    "receive_id_type": str(
                        send_result.raw.get("receive_id_type") or ""
                    ),
                    "chat_id": str(send_result.raw.get("chat_id") or ""),
                },
            )
            delivered_event_id = delivered.id
        return (
            Response()
            .ok(
                {
                    "sent": send_result.success,
                    "event_id": observed_event.id,
                    "delivered_event_id": delivered_event_id,
                    "provider_message_id": send_result.provider_message_id,
                    "reason": "" if send_result.success else send_result.error,
                    "light_feedback": {
                        "kind": "message_observed",
                        "event_id": observed_event.id,
                    },
                }
            )
            .__dict__
        )

    def _resolve_identity(self):
        session = (request.cookies.get(SESSION_COOKIE) or "").strip()
        if not session:
            return Response().error(f"{SESSION_COOKIE} cookie is required").__dict__
        store = PetLiveStore(self.dc_root / "data" / "feishu_pet.db")
        identity = store.get_identity_by_desktop_session_id(session)
        if identity is None:
            return Response().error("desktop session is not bound to a pet").__dict__
        return store, identity

    async def _send_workspace_message(
        self,
        *,
        identity: Any,
        conversation_id: str,
        content: str,
    ) -> TextSendResult:
        sender = self._message_sender
        if sender is None and _workspace_sender_enabled():
            sender = FeishuWorkspaceMessageSender()
        if sender is None:
            return TextSendResult(
                success=False,
                error="feishu_workspace_sender_not_configured",
            )
        receive_id = conversation_id or identity.feishu_open_id
        if not receive_id:
            return TextSendResult(
                success=False,
                error="receive_id is required",
            )
        receive_id_type = "chat_id" if receive_id.startswith("oc_") else "open_id"
        return await sender.send_text(
            receive_id,
            content,
            receive_id_type=receive_id_type,
        )

    async def _read_workspace_messages(
        self,
        *,
        identity: Any,
        conversation_id: str,
        events: list[StoredPetEvent],
    ) -> WorkspaceReadResult:
        reader = self._message_reader
        if reader is None and _workspace_reader_enabled():
            reader = FeishuWorkspaceMessageReader()
        if reader is None:
            return WorkspaceReadResult(
                success=False,
                error="feishu_workspace_reader_not_configured",
            )
        chat_id = (
            conversation_id
            if conversation_id.startswith("oc_")
            else _chat_id_for_conversation(events, conversation_id)
        )
        if not chat_id:
            return WorkspaceReadResult(
                success=False,
                error="chat_id_not_available",
            )
        return await reader.list_messages(
            chat_id=chat_id,
            conversation_id=conversation_id,
            identity=identity,
            limit=50,
        )


def _identity_user(identity: Any) -> dict[str, str]:
    return {
        "pet_id": identity.pet_id,
        "employee_id": identity.employee_id,
        "open_id": identity.feishu_open_id,
        "desktop_session_id": identity.desktop_session_id,
    }


def _workspace_conversations(
    identity: Any,
    events: list[StoredPetEvent],
) -> list[dict[str, Any]]:
    latest_by_conversation: dict[str, dict[str, Any]] = {}
    for event in events:
        message = _workspace_message_from_event(event)
        if message is None:
            continue
        conversation_id = str(message.get("conversation_id") or "")
        if not conversation_id:
            continue
        latest_by_conversation[conversation_id] = message

    if not latest_by_conversation:
        conversation_id = identity.feishu_open_id or identity.employee_id
        if not conversation_id:
            return []
        return [
            {
                "id": conversation_id,
                "title": "飞书小助手",
                "subtitle": "同一员工宠物身份",
                "last_message": "暂无桌面消息",
                "avatar": "助",
            }
        ]

    conversations: list[dict[str, Any]] = []
    for conversation_id, message in reversed(list(latest_by_conversation.items())):
        conversations.append(
            {
                "id": conversation_id,
                "title": "飞书小助手",
                "subtitle": "同一员工宠物身份",
                "last_message": str(message.get("content") or "")[:80],
                "avatar": "助",
            }
        )
    return conversations


def _workspace_messages_from_events(
    events: list[StoredPetEvent],
    *,
    conversation_id: str,
) -> list[dict[str, Any]]:
    messages = []
    for event in events:
        message = _workspace_message_from_event(event)
        if message is None:
            continue
        if str(message.get("conversation_id") or "") != conversation_id:
            continue
        messages.append(message)
    return messages


def _workspace_message_from_event(event: StoredPetEvent) -> dict[str, Any] | None:
    if event.source != "feishu_workspace":
        return None
    if event.event_type not in {
        "feishu_message_received",
        "assistant_response_sent",
        "assistant_message_observed",
        "assistant_response_failed",
    }:
        return None
    source_ref = event.source_ref.to_dict()
    conversation_id = str(source_ref.get("conversation_id") or "")
    payload = dict(event.payload or {})
    direction = str(payload.get("direction") or "")
    if event.event_type == "feishu_message_received":
        role = "user" if direction == "desktop_to_assistant" else "assistant"
        content = str(payload.get("content") or "").strip()
        if not content:
            text_len = int(payload.get("text_len") or 0)
            content = f"已发送消息（{text_len} 字）" if text_len else "已发送消息"
    elif event.event_type in {"assistant_response_sent", "assistant_message_observed"}:
        role = "assistant"
        content = str(payload.get("content") or "已发送到飞书小助手").strip()
    else:
        role = "assistant"
        content = str(payload.get("error") or payload.get("reason") or "消息发送失败")
    return {
        "id": f"pet_event_{event.id}",
        "role": role,
        "content": content,
        "created_at": event.created_at,
        "conversation_id": conversation_id,
        "event_id": event.id,
        "event_type": event.event_type,
        "provider_message_id": str(payload.get("provider_message_id") or ""),
    }


def _chat_id_for_conversation(
    events: list[StoredPetEvent],
    conversation_id: str,
) -> str:
    for event in reversed(events):
        if event.source != "feishu_workspace":
            continue
        source_ref = event.source_ref.to_dict()
        if str(source_ref.get("conversation_id") or "") != conversation_id:
            continue
        payload = dict(event.payload or {})
        chat_id = str(payload.get("chat_id") or "")
        if chat_id:
            return chat_id
    return ""


def _record_assistant_history_events(
    store: PetLiveStore,
    *,
    identity: Any,
    conversation_id: str,
    messages: list[dict[str, Any]],
    existing_events: list[StoredPetEvent],
) -> None:
    seen_provider_ids = _provider_message_ids_from_events(existing_events)
    min_created_at = _latest_workspace_event_epoch(existing_events, conversation_id)
    if min_created_at <= 0:
        return
    for message in messages:
        if message.get("role") != "assistant":
            continue
        provider_message_id = str(message.get("provider_message_id") or "")
        if not provider_message_id or provider_message_id in seen_provider_ids:
            continue
        message_created_at = _iso_to_epoch(str(message.get("created_at") or ""))
        if message_created_at <= min_created_at - 2:
            continue
        content = str(message.get("content") or "").strip()
        publish_pet_event(
            store,
            pet_id=identity.pet_id,
            user_id=identity.employee_id or identity.feishu_open_id,
            source="feishu_workspace",
            event_type="assistant_message_observed",
            source_ref={
                "employee_id": identity.employee_id,
                "platform": "feishu_history",
                "conversation_id": conversation_id,
                "message_id": provider_message_id,
                "desktop_session_id": identity.desktop_session_id,
            },
            payload={
                "employee_id": identity.employee_id,
                "direction": "feishu_history_observed",
                "content": content,
                "summary": content[:160] or "有小助手回复",
                "provider_message_id": provider_message_id,
                "chat_id": str(message.get("provider_chat_id") or ""),
                "msg_type": str(message.get("msg_type") or ""),
            },
        )
        seen_provider_ids.add(provider_message_id)


def _latest_workspace_event_epoch(
    events: list[StoredPetEvent],
    conversation_id: str,
) -> float:
    latest = 0.0
    for event in events:
        if event.source != "feishu_workspace":
            continue
        source_ref = event.source_ref.to_dict()
        if str(source_ref.get("conversation_id") or "") != conversation_id:
            continue
        created_at = _iso_to_epoch(event.created_at)
        if created_at > latest:
            latest = created_at
    return latest


def _provider_message_ids_from_events(events: list[StoredPetEvent]) -> set[str]:
    provider_ids: set[str] = set()
    for event in events:
        source_ref = event.source_ref.to_dict()
        source_message_id = str(source_ref.get("message_id") or "")
        if source_message_id:
            provider_ids.add(source_message_id)
        payload = dict(event.payload or {})
        payload_message_id = str(payload.get("provider_message_id") or "")
        if payload_message_id:
            provider_ids.add(payload_message_id)
    return provider_ids


def _iso_to_epoch(value: str) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _merge_workspace_messages(
    feishu_messages: list[dict[str, Any]],
    outbox_messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    provider_ids = {
        str(message.get("provider_message_id") or "")
        for message in feishu_messages
        if message.get("provider_message_id")
    }
    seen_ids: set[str] = set()
    for message in feishu_messages:
        message_id = str(message.get("id") or "")
        if message_id and message_id in seen_ids:
            continue
        if message_id:
            seen_ids.add(message_id)
        merged.append(message)
    for message in outbox_messages:
        provider_id = str(message.get("provider_message_id") or "")
        if provider_id and provider_id in provider_ids:
            continue
        message_id = str(message.get("id") or "")
        if message_id and message_id in seen_ids:
            continue
        if message_id:
            seen_ids.add(message_id)
        merged.append(message)
    return sorted(merged, key=lambda message: str(message.get("created_at") or ""))


def _feishu_message_to_workspace_message(
    item: Any,
    *,
    conversation_id: str,
    app_id: str,
) -> dict[str, Any] | None:
    message_id = str(getattr(item, "message_id", "") or "")
    if not message_id:
        return None
    msg_type = str(getattr(item, "msg_type", "") or "")
    body = getattr(item, "body", None)
    raw_content = str(getattr(body, "content", "") or "") if body else ""
    content = _feishu_content_text(msg_type, raw_content)
    sender = getattr(item, "sender", None)
    sender_id = str(getattr(sender, "id", "") or "") if sender else ""
    sender_type = str(getattr(sender, "sender_type", "") or "") if sender else ""
    role = "assistant" if sender_type == "app" or sender_id == app_id else "user"
    created_at = _feishu_timestamp_to_iso(getattr(item, "create_time", 0) or 0)
    return {
        "id": message_id,
        "role": role,
        "content": content,
        "created_at": created_at,
        "conversation_id": conversation_id,
        "provider_message_id": message_id,
        "provider_chat_id": str(getattr(item, "chat_id", "") or ""),
        "msg_type": msg_type,
        "source": "feishu_history",
    }


def _feishu_content_text(msg_type: str, raw_content: str) -> str:
    if msg_type == "text":
        try:
            parsed = json.loads(raw_content)
        except json.JSONDecodeError:
            return raw_content
        if isinstance(parsed, dict) and isinstance(parsed.get("text"), str):
            return parsed["text"]
        return ""
    if msg_type:
        return f"[{msg_type} 消息]"
    return "[消息]"


def _feishu_timestamp_to_iso(value: Any) -> str:
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        timestamp = 0
    if timestamp > 10_000_000_000:
        timestamp = timestamp // 1000
    if timestamp <= 0:
        return ""
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def _workspace_integration_status() -> str:
    return (
        "feishu_workspace_sender_enabled"
        if _workspace_sender_enabled()
        else "feishu_workspace_sender_not_configured"
    )


def _workspace_sender_enabled() -> bool:
    return os.environ.get("WORKSPACE_FEISHU_SEND_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _workspace_reader_enabled() -> bool:
    return os.environ.get("WORKSPACE_FEISHU_READ_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _workspace_read_lookback_minutes() -> int:
    try:
        value = int(os.environ.get("WORKSPACE_FEISHU_READ_LOOKBACK_MINUTES", "1440"))
    except ValueError:
        value = 1440
    return max(1, min(value, 60 * 24 * 7))
