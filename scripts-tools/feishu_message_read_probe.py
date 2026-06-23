#!/usr/bin/env python3
"""Probe Feishu IM message history read capability.

Default mode is read-only and redacts message content. When only an open_id is
available, pass --send-probe-text together with --allow-real-feishu-send to
create one explicit probe message and use the returned chat_id for the history
read check.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "dc_engines"))


def _parse_text_content(content: str) -> str:
    if not content:
        return ""
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return content
    if isinstance(parsed, dict):
        text = parsed.get("text")
        if isinstance(text, str):
            return text
    return ""


def _redacted_text_summary(content: str) -> str:
    text = _parse_text_content(content)
    return f"<redacted chars={len(text)}>" if text else "<non-text-or-empty>"


def _message_to_summary(item: Any, *, show_text: bool) -> dict[str, Any]:
    body = getattr(item, "body", None)
    content = str(getattr(body, "content", "") or "") if body else ""
    sender = getattr(item, "sender", None)
    sender_id = str(getattr(sender, "id", "") or "") if sender else ""
    sender_type = str(getattr(sender, "sender_type", "") or "") if sender else ""
    text = _parse_text_content(content)
    return {
        "message_id": str(getattr(item, "message_id", "") or ""),
        "chat_id": str(getattr(item, "chat_id", "") or ""),
        "msg_type": str(getattr(item, "msg_type", "") or ""),
        "create_time": int(getattr(item, "create_time", 0) or 0),
        "sender_type": sender_type,
        "sender_id": sender_id,
        "content": text if show_text else _redacted_text_summary(content),
    }


async def _send_probe_message(
    *,
    client: Any,
    receive_id: str,
    receive_id_type: str,
    text: str,
) -> tuple[bool, str, str, str]:
    from dc_engines.feishu_hub import get_hub
    from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

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
        return False, "", "", f"{type(exc).__name__}: {exc}"
    if not resp.success():
        return (
            False,
            "",
            "",
            f"Feishu API error code={getattr(resp, 'code', '?')} msg={getattr(resp, 'msg', '?')}",
        )
    data = getattr(resp, "data", None)
    return (
        True,
        str(getattr(data, "message_id", "") or "") if data else "",
        str(getattr(data, "chat_id", "") or "") if data else "",
        "",
    )


async def _list_messages(
    *,
    client: Any,
    container_id_type: str,
    container_id: str,
    start_time: int,
    end_time: int,
    page_size: int,
) -> tuple[bool, str, list[Any], bool, str]:
    from dc_engines.feishu_hub import get_hub
    from lark_oapi.api.im.v1 import ListMessageRequest

    req = (
        ListMessageRequest.builder()
        .container_id_type(container_id_type)
        .container_id(container_id)
        .start_time(str(start_time))
        .end_time(str(end_time))
        .sort_type("ByCreateTimeAsc")
        .page_size(page_size)
        .build()
    )
    try:
        resp = await client.im.v1.message.alist(req)
        get_hub().record_call("im.message.list")
    except Exception as exc:  # noqa: BLE001
        get_hub().record_call("im.message.list", error=exc)
        return False, f"{type(exc).__name__}: {exc}", [], False, ""
    if not resp.success():
        return (
            False,
            f"Feishu API error code={getattr(resp, 'code', '?')} msg={getattr(resp, 'msg', '?')}",
            [],
            False,
            "",
        )
    data = getattr(resp, "data", None)
    items = list(getattr(data, "items", None) or []) if data else []
    return (
        True,
        "",
        items,
        bool(getattr(data, "has_more", False)) if data else False,
        str(getattr(data, "page_token", "") or "") if data else "",
    )


async def _amain(args: argparse.Namespace) -> int:
    if args.send_probe_text and not args.allow_real_feishu_send:
        print(
            json.dumps(
                {
                    "ok": False,
                    "stage": "send_probe",
                    "error": "--send-probe-text requires --allow-real-feishu-send",
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2

    from dc_engines.feishu_hub import get_client

    client = get_client()
    if client is None:
        print(
            json.dumps(
                {
                    "ok": False,
                    "stage": "credentials",
                    "error": "Feishu credentials disabled",
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2

    container_id = args.container_id.strip()
    container_id_type = args.container_id_type.strip()
    sent_message_id = ""
    discovered_chat_id = ""
    if args.send_probe_text:
        ok, sent_message_id, discovered_chat_id, error = await _send_probe_message(
            client=client,
            receive_id=args.receive_id.strip() or container_id,
            receive_id_type=args.receive_id_type.strip(),
            text=args.probe_text,
        )
        if not ok:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "stage": "send_probe",
                        "error": error,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 2
        if not container_id and discovered_chat_id:
            container_id = discovered_chat_id
            container_id_type = "chat"

    if not container_id:
        print(
            json.dumps(
                {
                    "ok": False,
                    "stage": "input",
                    "error": "container_id is required unless --send-probe-text discovers a chat_id",
                    "sent_message_id": sent_message_id,
                    "discovered_chat_id": discovered_chat_id,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2

    end_time = int(time.time())
    start_time = end_time - int(args.lookback_minutes * 60)
    ok, error, items, has_more, page_token = await _list_messages(
        client=client,
        container_id_type=container_id_type,
        container_id=container_id,
        start_time=start_time,
        end_time=end_time,
        page_size=args.page_size,
    )
    if not ok:
        print(
            json.dumps(
                {
                    "ok": False,
                    "stage": "list_messages",
                    "container_id_type": container_id_type,
                    "container_id": container_id,
                    "sent_message_id": sent_message_id,
                    "discovered_chat_id": discovered_chat_id,
                    "error": error,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2

    summaries = [_message_to_summary(item, show_text=args.show_text) for item in items]
    type_counts = Counter(str(item.get("msg_type") or "") for item in summaries)
    contains_sent_probe = bool(
        sent_message_id
        and any(item.get("message_id") == sent_message_id for item in summaries)
    )
    print(
        json.dumps(
            {
                "ok": True,
                "container_id_type": container_id_type,
                "container_id": container_id,
                "sent_message_id": sent_message_id,
                "discovered_chat_id": discovered_chat_id,
                "contains_sent_probe": contains_sent_probe,
                "count": len(summaries),
                "msg_type_counts": dict(type_counts),
                "has_more": has_more,
                "page_token_present": bool(page_token),
                "messages": summaries[: args.output_limit],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--container-id", default="")
    parser.add_argument("--container-id-type", default="chat")
    parser.add_argument("--receive-id", default="")
    parser.add_argument("--receive-id-type", default="open_id")
    parser.add_argument("--send-probe-text", action="store_true")
    parser.add_argument(
        "--allow-real-feishu-send",
        action="store_true",
        help="Required with --send-probe-text because it posts a real Feishu message.",
    )
    parser.add_argument(
        "--probe-text",
        default="Feishu message read probe: desktop workspace history",
    )
    parser.add_argument("--lookback-minutes", type=int, default=30)
    parser.add_argument("--page-size", type=int, default=20)
    parser.add_argument("--output-limit", type=int, default=5)
    parser.add_argument("--show-text", action="store_true")
    args = parser.parse_args(argv)
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
