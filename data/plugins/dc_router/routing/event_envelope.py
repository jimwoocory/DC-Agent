"""Pure envelope construction: AstrMessageEvent → dc_router_core.MessageEnvelope.

不做任何 set_provider / 写回 event 的副作用；只把 event 状态提取成 immutable 数据结构。
下游 routing 模块只用 envelope，不再耦合 AstrBot 类型（除了极少量属性访问）。
"""

from __future__ import annotations

from typing import Any

from dc_router_core.entrypoint import MessageEnvelope
from dc_router_core.taxonomy import AttachmentKind

from ..config import is_business_platform, is_ops_platform


def _platform_id(event: Any) -> str:
    getter = getattr(event, "get_platform_id", None)
    if not callable(getter):
        return ""
    try:
        raw = getter()
    except Exception:  # noqa: BLE001
        return ""
    return str(raw or "")


def _sender_id(event: Any) -> str:
    getter = getattr(event, "get_sender_id", None)
    if not callable(getter):
        return ""
    try:
        return str(getter() or "")
    except Exception:  # noqa: BLE001
        return ""


def _message_text(event: Any) -> str:
    try:
        text = event.message_str or ""
    except Exception:  # noqa: BLE001
        text = ""
    return str(text).strip() if text else ""


def _components(event: Any) -> list[Any]:
    try:
        message = event.message_obj.message
    except Exception:  # noqa: BLE001
        return []
    return message if isinstance(message, list) else []


def _attachment_kinds(event: Any) -> tuple[AttachmentKind, ...]:
    """Best-effort map of message components → AttachmentKind.

    不 import astrbot 全套组件类型（避免 plugin 启动期硬依赖）；
    用 duck-typing 识别 Image / Record / Video / File。
    """
    from astrbot.api.message_components import File, Image, Record, Video

    kinds: list[AttachmentKind] = []
    for comp in _components(event):
        if isinstance(comp, Image):
            kinds.append(AttachmentKind.IMAGE)
        elif isinstance(comp, Record):
            kinds.append(AttachmentKind.VOICE)
        elif isinstance(comp, Video):
            kinds.append(AttachmentKind.VIDEO)
        elif isinstance(comp, File):
            kinds.append(AttachmentKind.FILE)
    return tuple(kinds)


def build_envelope(event: Any) -> MessageEnvelope:
    """把 AstrMessageEvent 提取为 MessageEnvelope（纯函数，无副作用）。"""
    text = _message_text(event)
    kinds = _attachment_kinds(event)
    platform_id = _platform_id(event)
    sender_id = _sender_id(event)

    metadata: dict[str, str] = {
        "platform_id": platform_id,
        "umo": getattr(event, "unified_msg_origin", "") or "",
    }
    if is_business_platform(platform_id):
        metadata["router_mode"] = "business"
    elif is_ops_platform(platform_id):
        metadata["router_mode"] = "ops"

    get_extra = getattr(event, "get_extra", None)
    if callable(get_extra):
        for key in ("feishu_channel_agent_id", "reasoning_tier"):
            value = get_extra(key)
            if value:
                metadata[key] = str(value)

    return MessageEnvelope(
        text=text,
        attachment_kinds=kinds,
        attachment_summary=None,
        user_id=sender_id or None,
        session_id=getattr(event, "unified_msg_origin", "") or None,
        metadata=metadata,
    )


__all__ = ["build_envelope"]
