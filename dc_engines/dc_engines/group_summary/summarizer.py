"""LLM-backed summarization of a normalized group message stream.

Pure-ish function: takes a list of messages and a Provider, returns a
GroupSummary. No platform / DB knowledge — that's history_fetcher's job.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from .contracts import GroupMessage, GroupSummary
from .time_range import TimeRange

if TYPE_CHECKING:
    from astrbot.core.provider.provider import Provider


GROUP_SUMMARY_SYSTEM_PROMPT = """你是一个企业项目群聊摘要助手。给你一组聊天记录，请按下面四段输出，每段 1-3 句话。如果某一段没有内容，写"（无）"。

请严格输出 JSON：
{
  "client_feedback": "...",
  "boss_requests": "...",
  "execution_issues": "...",
  "stage_conclusions": "..."
}

四段说明：
- client_feedback：来自甲方/客户的反馈、要求、不满
- boss_requests：来自老板/上级的指示、要求、决策
- execution_issues：团队执行中遇到的问题、阻塞、缺资源
- stage_conclusions：本时段达成的结论、已确认的事项、下一步动作
"""


# 单次发给 LLM 的最大消息数，防止上下文爆炸。比 default max_count 略大留余量。
_LLM_MESSAGE_CEILING = 200
_EMPTY_SECTION = "（无）"


def _empty_summary(time_range: TimeRange, *, raw: str = "") -> GroupSummary:
    return GroupSummary(
        client_feedback=_EMPTY_SECTION,
        boss_requests=_EMPTY_SECTION,
        execution_issues=_EMPTY_SECTION,
        stage_conclusions=_EMPTY_SECTION,
        message_count=0,
        time_range=time_range.description,
        raw_response=raw,
    )


def _serialize_messages(messages: list[GroupMessage]) -> str:
    """渲染消息列表为 LLM 输入文本。一行一条，含发送者 + 时间 + 内容。"""
    lines: list[str] = []
    for msg in messages:
        ts = msg["timestamp"]
        try:
            ts_str = ts.strftime("%m-%d %H:%M")
        except Exception:
            ts_str = str(ts)
        sender = msg.get("sender") or msg.get("sender_id") or "?"
        content = (msg.get("content") or "").strip().replace("\n", " ")
        if content:
            lines.append(f"[{ts_str}] {sender}: {content}")
    return "\n".join(lines)


def _extract_json_object(raw: str) -> dict[str, Any] | None:
    """LLM 经常套 ```json ... ``` fence 或前后多文本；尽量挖出 JSON 对象。"""
    if not raw:
        return None
    text = raw.strip()
    # 1) 直接尝试
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    # 2) 代码块
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fence:
        try:
            data = json.loads(fence.group(1))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    # 3) 第一个 {...} 块
    bare = re.search(r"\{.*\}", text, re.S)
    if bare:
        try:
            data = json.loads(bare.group(0))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return None


def _coerce_section(value: Any) -> str:
    if value is None:
        return _EMPTY_SECTION
    if isinstance(value, str):
        s = value.strip()
        return s if s else _EMPTY_SECTION
    if isinstance(value, list):
        joined = "; ".join(str(x).strip() for x in value if str(x).strip())
        return joined if joined else _EMPTY_SECTION
    return str(value).strip() or _EMPTY_SECTION


async def summarize_group_chat(
    messages: list[GroupMessage],
    *,
    llm_provider: Provider,
    time_range: TimeRange,
) -> GroupSummary:
    """Ask the LLM to bucket ``messages`` into the four-section summary.

    Robustness contract:

    - Empty ``messages`` returns a GroupSummary with all four sections set to
      "（无）" and ``message_count == 0`` (no LLM call).
    - LLM returning malformed JSON, partial keys, or wrapping in code fences
      degrades gracefully: missing sections become "（无）", and the raw
      payload is preserved in ``raw_response``.
    - Long inputs are truncated to a configured token / message ceiling
      before being sent to the LLM. Truncation is from the head (older
      messages dropped first) so the tail's "conclusions" survive.
    """
    if not messages:
        return _empty_summary(time_range)

    # 截断：从头丢老的，保留最近的尾巴（更可能含 stage_conclusions）
    if len(messages) > _LLM_MESSAGE_CEILING:
        trimmed = messages[-_LLM_MESSAGE_CEILING:]
    else:
        trimmed = list(messages)

    prompt = _serialize_messages(trimmed)
    raw_response = ""

    try:
        resp = await llm_provider.text_chat(
            prompt=prompt,
            system_prompt=GROUP_SUMMARY_SYSTEM_PROMPT,
        )
        raw_response = (
            getattr(resp, "completion_text", "")
            or getattr(resp, "raw_response", "")
            or ""
        )
    except Exception:
        # LLM 失败：返回空 summary 而非抛错，由 RouterStage 决定怎么提示用户
        return _empty_summary(time_range, raw="<llm_call_failed>")

    parsed = _extract_json_object(raw_response)

    if parsed is None:
        # 解析失败也别抛错
        summary = _empty_summary(time_range, raw=raw_response)
        # 但 message_count 至少要诚实记录传给 LLM 的数量
        return GroupSummary(
            client_feedback=summary.client_feedback,
            boss_requests=summary.boss_requests,
            execution_issues=summary.execution_issues,
            stage_conclusions=summary.stage_conclusions,
            message_count=len(messages),
            time_range=time_range.description,
            raw_response=raw_response,
        )

    return GroupSummary(
        client_feedback=_coerce_section(parsed.get("client_feedback")),
        boss_requests=_coerce_section(parsed.get("boss_requests")),
        execution_issues=_coerce_section(parsed.get("execution_issues")),
        stage_conclusions=_coerce_section(parsed.get("stage_conclusions")),
        message_count=len(messages),
        time_range=time_range.description,
        raw_response=raw_response,
    )
