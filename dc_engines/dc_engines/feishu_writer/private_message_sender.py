"""Send one-to-one Feishu private messages."""

from __future__ import annotations

import json
import logging
from typing import Any

from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

from dc_engines.employee_insight_loop import TextSendResult
from dc_engines.feishu_hub import get_client, get_hub

logger = logging.getLogger(__name__)


class FeishuPrivateMessageSender:
    """Small sender adapter for Feishu open_id private messages."""

    def __init__(self, client: Any | None = None) -> None:
        self._client = client if client is not None else get_client()

    @property
    def enabled(self) -> bool:
        return self._client is not None

    async def send_text(self, employee_id: str, text: str) -> TextSendResult:
        """Send a text message to one Feishu open_id."""
        open_id = (employee_id or "").strip()
        message_text = (text or "").strip()
        if not self.enabled:
            return TextSendResult(
                success=False,
                error="Feishu credentials disabled",
                raw={"employee_id": open_id},
            )
        if not open_id:
            return TextSendResult(success=False, error="employee_id is required")
        if not message_text:
            return TextSendResult(
                success=False,
                error="message text is required",
                raw={"employee_id": open_id},
            )

        return await self._send_message(
            open_id=open_id,
            msg_type="text",
            content=json.dumps({"text": message_text}, ensure_ascii=False),
        )

    async def send_interactive_card(
        self,
        employee_id: str,
        card: dict[str, Any],
        *,
        card_type: str = "employee_insight_welcome",
    ) -> TextSendResult:
        """Send an interactive card message to one Feishu open_id."""
        open_id = (employee_id or "").strip()
        if not self.enabled:
            return TextSendResult(
                success=False,
                error="Feishu credentials disabled",
                raw={"employee_id": open_id, "msg_type": "interactive"},
            )
        if not open_id:
            return TextSendResult(success=False, error="employee_id is required")
        if not card:
            return TextSendResult(
                success=False,
                error="card is required",
                raw={"employee_id": open_id, "msg_type": "interactive"},
            )

        raw = {
            "employee_id": open_id,
            "msg_type": "interactive",
            "card_type": card_type,
        }
        try:
            from dc_engines.card_runtime import send_card_via_runtime
            from dc_engines.feishu_card_streamer import FeishuCardStreamer

            stream = await send_card_via_runtime(
                FeishuCardStreamer(self._client),
                card_type=card_type,
                chat_id=open_id,
                receive_id_type="open_id",
                card=card,
                platform_id="巅池-Agent小助手",
                event="employee_insight_outreach",
                detail="employee insight welcome card",
            )
        except Exception as exc:  # noqa: BLE001
            get_hub().record_call("im.message.create", error=exc)
            logger.warning("[feishu_writer] send private card exception: %s", exc)
            return TextSendResult(
                success=False,
                error=f"{type(exc).__name__}: {exc}",
                raw=raw,
            )

        if stream is None:
            get_hub().record_call(
                "im.message.create",
                error=RuntimeError("card runtime send failed"),
            )
            return TextSendResult(
                success=False,
                error="Feishu card runtime send failed",
                raw=raw,
            )

        get_hub().record_call("im.message.create")
        return TextSendResult(
            success=True,
            provider_message_id=stream.message_id,
            raw=raw,
        )

    async def _send_message(
        self,
        *,
        open_id: str,
        msg_type: str,
        content: str,
    ) -> TextSendResult:
        req = (
            CreateMessageRequest.builder()
            .receive_id_type("open_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(open_id)
                .msg_type(msg_type)
                .content(content)
                .build()
            )
            .build()
        )
        try:
            resp = await self._client.im.v1.message.acreate(req)
            get_hub().record_call("im.message.create")
        except Exception as exc:  # noqa: BLE001
            get_hub().record_call("im.message.create", error=exc)
            logger.warning("[feishu_writer] send private message exception: %s", exc)
            return TextSendResult(
                success=False,
                error=f"{type(exc).__name__}: {exc}",
                raw={"employee_id": open_id, "msg_type": msg_type},
            )

        if not resp.success():
            code = getattr(resp, "code", "?")
            msg = getattr(resp, "msg", "?")
            return TextSendResult(
                success=False,
                error=f"Feishu API error code={code} msg={msg}",
                raw={
                    "employee_id": open_id,
                    "msg_type": msg_type,
                    "code": code,
                    "msg": msg,
                },
            )

        message_id = getattr(resp.data, "message_id", "") if resp.data else ""
        return TextSendResult(
            success=True,
            provider_message_id=str(message_id or ""),
            raw={"employee_id": open_id, "msg_type": msg_type},
        )
