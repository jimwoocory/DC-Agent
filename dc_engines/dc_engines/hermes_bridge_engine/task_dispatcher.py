"""Hermes task webhook dispatcher adapter."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)


class HermesTaskDispatcher:
    """Send standard Harness workflow tasks to a Hermes task webhook."""

    def __init__(self, *, task_webhook_url: str, secret: str) -> None:
        self.task_webhook_url = task_webhook_url
        self.secret = secret

    async def dispatch(
        self,
        task_id: str,
        workflow_kind: str,
        brief: str,
        umo: str,
        cognitive_context: dict[str, Any],
        extra_payload: dict[str, Any] | None = None,
    ) -> bool:
        payload = {
            "task_id": task_id,
            "workflow_kind": workflow_kind,
            "brief": brief,
            "session_id": umo,
            "unified_msg_origin": umo,
            "cognitive_context": cognitive_context,
        }
        if extra_payload:
            payload.update(extra_payload)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        signature = self._sign(body)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.task_webhook_url,
                    data=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-Hub-Signature-256": signature,
                        "X-Webhook-Event": "harness_task",
                        "X-Task-ID": task_id,
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status in (200, 201, 202):
                        logger.info(
                            "[HermesTaskDispatcher] task %s dispatched to %s",
                            task_id,
                            self.task_webhook_url,
                        )
                        return True
                    logger.warning(
                        "[HermesTaskDispatcher] dispatch failed HTTP %s: %s",
                        resp.status,
                        await resp.text(),
                    )
                    return False
        except Exception as exc:  # noqa: BLE001
            logger.warning("[HermesTaskDispatcher] dispatch exception: %s", exc)
            return False

    def _sign(self, body: bytes) -> str:
        digest = hmac.new(self.secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        return f"sha256={digest}"
