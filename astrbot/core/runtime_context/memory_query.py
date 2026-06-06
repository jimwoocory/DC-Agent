from __future__ import annotations

import json
from typing import Any

from astrbot.api import logger

MEMORY_QUERY_HISTORY_TURNS = 6
MEMORY_QUERY_HISTORY_MAX_CHARS = 1600


def stringify_history_content(content: object) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text") or item.get("content") or ""
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
            elif isinstance(item, str) and item.strip():
                parts.append(item.strip())
        return " ".join(parts).strip()
    return ""


def recent_history_text_for_memory_query(history_json: str) -> str:
    try:
        history = json.loads(history_json or "[]")
    except json.JSONDecodeError:
        return ""
    if not isinstance(history, list):
        return ""

    lines: list[str] = []
    for item in history[-MEMORY_QUERY_HISTORY_TURNS:]:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip()
        if role not in {"user", "assistant"}:
            continue
        content = stringify_history_content(item.get("content"))
        if not content:
            continue
        label = "用户" if role == "user" else "助手"
        lines.append(f"{label}: {content}")

    text = "\n".join(lines).strip()
    if len(text) > MEMORY_QUERY_HISTORY_MAX_CHARS:
        text = text[-MEMORY_QUERY_HISTORY_MAX_CHARS:]
    return text


async def build_memory_retrieval_query(context: Any, event: Any) -> str:
    current_text = (getattr(event, "message_str", "") or "").strip()
    conv_mgr = getattr(context, "conversation_manager", None)
    if conv_mgr is None:
        return current_text

    try:
        cid = await conv_mgr.get_curr_conversation_id(event.unified_msg_origin)
        if not cid:
            return current_text
        conversation = await conv_mgr.get_conversation(event.unified_msg_origin, cid)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[runtime_context] memory query history lookup skipped: %s", exc)
        return current_text

    history_text = recent_history_text_for_memory_query(
        getattr(conversation, "history", "") if conversation else ""
    )
    if not history_text:
        return current_text
    return f"最近对话：\n{history_text}\n\n当前消息：\n{current_text}"
