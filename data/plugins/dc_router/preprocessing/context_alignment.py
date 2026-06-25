"""Context alignment guard for quoted failed/internal context."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from astrbot.api import logger
from astrbot.api.event import MessageEventResult

INTERNAL_CONTEXT_RE = re.compile(
    r"<dc_truth_source\b|<dc_agent_memory_context\b",
    re.IGNORECASE,
)
FAILED_CONTEXT_RE = re.compile(
    r"(没有通过证据校验|已拦截发送|Brave\s*搜索服务|检索结果是否为空|"
    r"目标回调服务器超时未响应|任务失败|生成失败|媒体生成任务失败)",
    re.IGNORECASE,
)
FOLLOWUP_RE = re.compile(
    r"(继续|重试|再试|重新|接着|按上面|按刚才|刚才那|上面那|这条|这个问题|"
    r"这个报告|这个任务|这张图|这份材料|沿用|复用)",
    re.IGNORECASE,
)
NEW_TASK_RE = re.compile(
    r"^\s*(这是|作为|按|当成)?\s*(新任务|新需求|新话题)\s*[:：,，]?", re.I
)
NEW_SESSION_RE = re.compile(r"^\s*/?(new\s+session|新会话|开启新话题)\s*$", re.I)

CONTEXT_ALIGNMENT_PROMPT = (
    "我检测到你这条消息回复的是上一条失败或旧任务上下文，但当前内容看起来像新的任务。"
    "IM 只有一个固定聊天窗口，为避免串任务，我先不继续处理。\n\n"
    "你可以：\n"
    "1. 如果要继续上一条任务，请回复“继续上一条”或“重试上一条”。\n"
    "2. 如果这是新任务，请回复“新任务：”再接完整需求。"
)


@dataclass(slots=True)
class ContextAlignmentDecision:
    stop: bool = False
    reason: str = ""
    quoted_text: str = ""


def try_handle_context_alignment(
    event: Any,
    *,
    raw_text: str,
) -> ContextAlignmentDecision:
    """Stop when the user replies to stale failed/internal context with a new task."""

    text = (raw_text or "").strip()
    if (
        not text
        or NEW_SESSION_RE.match(text)
        or FOLLOWUP_RE.search(text)
        or NEW_TASK_RE.match(text)
    ):
        return ContextAlignmentDecision()

    quoted_text = _quoted_text(event)
    if not quoted_text:
        return ContextAlignmentDecision()

    reason = ""
    if INTERNAL_CONTEXT_RE.search(quoted_text):
        reason = "quoted_internal_context"
    elif FAILED_CONTEXT_RE.search(quoted_text):
        reason = "quoted_failed_context"
    if not reason:
        return ContextAlignmentDecision()

    try:
        event.should_call_llm(False)
        event.set_extra("dc_context_alignment_warning", reason)
        event.set_result(
            MessageEventResult()
            .message(CONTEXT_ALIGNMENT_PROMPT)
            .use_t2i(False)
            .stop_event()
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[dc_router] context alignment prompt failed: %s", exc)
        return ContextAlignmentDecision()

    logger.info(
        "[dc_router] context alignment stopped reason=%s text=%r quote=%r",
        reason,
        text[:80],
        quoted_text[:80],
    )
    return ContextAlignmentDecision(
        stop=True,
        reason=reason,
        quoted_text=quoted_text,
    )


def _quoted_text(event: Any) -> str:
    message_obj = getattr(event, "message_obj", None)
    components = list(getattr(message_obj, "message", None) or [])
    parts: list[str] = []
    for comp in components:
        if _is_reply_component(comp):
            parts.append(_component_text(comp))
    return "\n".join(part for part in parts if part).strip()


def _is_reply_component(comp: Any) -> bool:
    return comp.__class__.__name__.lower() == "reply" or bool(
        getattr(comp, "message_str", None)
        and (
            hasattr(comp, "sender_id")
            or hasattr(comp, "sender_nickname")
            or hasattr(comp, "chain")
        )
    )


def _component_text(comp: Any) -> str:
    direct = str(
        getattr(comp, "message_str", None) or getattr(comp, "text", None) or ""
    ).strip()
    parts = [direct] if direct else []
    for child in list(getattr(comp, "chain", None) or []):
        child_text = str(
            getattr(child, "text", None) or getattr(child, "message_str", None) or ""
        ).strip()
        if child_text:
            parts.append(child_text)
    return "\n".join(parts).strip()


__all__ = [
    "CONTEXT_ALIGNMENT_PROMPT",
    "ContextAlignmentDecision",
    "try_handle_context_alignment",
]
