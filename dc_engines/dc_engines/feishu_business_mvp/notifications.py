"""Business notification sender for the Feishu business MVP."""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from dc_engines.feishu_hub import call as hub_call
from dc_engines.feishu_hub import get_client, is_enabled

from .contracts import NotificationLog
from .store import BusinessMvpStore

CallFn = Callable[[str, Awaitable[Any]], Awaitable[Any]]


class BusinessNotifier:
    """Send Feishu business notifications and persist audit logs."""

    def __init__(
        self,
        *,
        store: BusinessMvpStore | None = None,
        client: Any | None = None,
        enabled: bool | None = None,
        call_fn: CallFn | None = None,
    ) -> None:
        """Create a business notifier.

        Args:
            store: Optional local notification log store.
            client: Optional injected lark client for tests.
            enabled: Optional enabled flag. Defaults to feishu_hub.is_enabled().
            call_fn: Optional injected async call wrapper. Defaults to feishu_hub.call.
        """

        self.store = store
        self._client = client if client is not None else get_client()
        self._enabled = bool(is_enabled() if enabled is None else enabled)
        self._call = call_fn or hub_call

    @property
    def enabled(self) -> bool:
        """Return whether live Feishu message calls can be issued.

        Returns:
            True when enabled and a client is available.
        """

        return bool(self._enabled and self._client is not None)

    async def send_text(
        self,
        *,
        target_id: str,
        text: str,
        receive_id_type: str = "open_id",
        notification_type: str = "business_text",
    ) -> NotificationLog:
        """Send a Feishu text message.

        Args:
            target_id: Recipient ID.
            text: Message text.
            receive_id_type: Feishu receive ID type.
            notification_type: Business notification type.

        Returns:
            Notification audit log.
        """

        return await self._send_message(
            target_id=target_id,
            receive_id_type=receive_id_type,
            msg_type="text",
            content={"text": text},
            notification_type=notification_type,
        )

    async def send_card(
        self,
        *,
        target_id: str,
        card: dict[str, Any],
        receive_id_type: str = "open_id",
        notification_type: str = "business_card",
    ) -> NotificationLog:
        """Send a Feishu interactive card.

        Args:
            target_id: Recipient ID.
            card: Interactive card JSON.
            receive_id_type: Feishu receive ID type.
            notification_type: Business notification type.

        Returns:
            Notification audit log.
        """

        return await self._send_message(
            target_id=target_id,
            receive_id_type=receive_id_type,
            msg_type="interactive",
            content=card,
            notification_type=notification_type,
        )

    async def _send_message(
        self,
        *,
        target_id: str,
        receive_id_type: str,
        msg_type: str,
        content: dict[str, Any],
        notification_type: str,
    ) -> NotificationLog:
        """Send a message and persist one audit log row.

        Args:
            target_id: Recipient ID.
            receive_id_type: Feishu receive ID type.
            msg_type: Feishu message type.
            content: Message content body.
            notification_type: Business notification type.

        Returns:
            Notification audit log.
        """

        payload = {
            "receive_id_type": receive_id_type,
            "msg_type": msg_type,
            "content": content,
        }
        if not target_id:
            return await self._record(
                target_type=receive_id_type,
                target_id=target_id,
                notification_type=notification_type,
                send_status="skipped",
                fail_reason="missing target_id",
                payload=payload,
            )
        if not self.enabled:
            return await self._record(
                target_type=receive_id_type,
                target_id=target_id,
                notification_type=notification_type,
                send_status="disabled",
                fail_reason="feishu hub disabled",
                payload=payload,
            )

        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

        body = (
            CreateMessageRequestBody.builder()
            .receive_id(target_id)
            .msg_type(msg_type)
            .content(json.dumps(content, ensure_ascii=False))
            .build()
        )
        req = (
            CreateMessageRequest.builder()
            .receive_id_type(receive_id_type)
            .request_body(body)
            .build()
        )

        try:
            resp = await self._call(
                "im.message.create",
                self._client.im.v1.message.acreate(req),
            )
            success = getattr(resp, "success", None)
            if callable(success) and not success():
                return await self._record(
                    target_type=receive_id_type,
                    target_id=target_id,
                    notification_type=notification_type,
                    send_status="failed",
                    fail_reason=f"code={getattr(resp, 'code', '?')} msg={getattr(resp, 'msg', '')}",
                    payload=payload,
                )
        except Exception as exc:  # noqa: BLE001
            return await self._record(
                target_type=receive_id_type,
                target_id=target_id,
                notification_type=notification_type,
                send_status="failed",
                fail_reason=f"{type(exc).__name__}: {exc}",
                payload=payload,
            )

        return await self._record(
            target_type=receive_id_type,
            target_id=target_id,
            notification_type=notification_type,
            send_status="sent",
            fail_reason="",
            payload=payload,
        )

    async def _record(
        self,
        *,
        target_type: str,
        target_id: str,
        notification_type: str,
        send_status: str,
        fail_reason: str,
        payload: dict[str, Any],
    ) -> NotificationLog:
        """Persist or return a notification log.

        Args:
            target_type: Recipient ID type.
            target_id: Recipient ID.
            notification_type: Business notification type.
            send_status: Send status.
            fail_reason: Failure reason if any.
            payload: Notification payload.

        Returns:
            NotificationLog.
        """

        log = NotificationLog(
            notification_id=uuid.uuid4().hex,
            target_type=target_type,
            target_id=target_id,
            notification_type=notification_type,
            send_status=send_status,
            fail_reason=fail_reason,
            payload=payload,
        )
        if self.store is not None:
            return await self.store.record_notification(log)
        return log
