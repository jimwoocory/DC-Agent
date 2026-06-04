"""LLM-backed 任务抽取。

把一组聊天消息扔给 LLM，让它输出 JSON 数组（每个对象是一条待办任务），
然后用 ``deadline_parser`` 把时间字符串解析成 datetime，结果是 ``ExtractedTask`` 列表。

容错原则（沿用 group_summary 的范式）：
- LLM 失败 / JSON 解析失败 / 字段缺失 → 返回空列表（绝不抛异常）
- 个别任务字段缺失 → 该字段为 None / 默认值
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import TYPE_CHECKING, Any

from .contracts import ExtractedTask
from .deadline_parser import parse_deadline

if TYPE_CHECKING:
    from astrbot.core.provider.provider import Provider


TASK_EXTRACTOR_SYSTEM_PROMPT = """你是企业项目群聊任务抽取助手。给你一组聊天记录，请抽出其中含的"待办任务"。
一条聊天可能不含任务、含一个任务、或含多个任务。

只返回 JSON 数组，每个任务对象字段如下（按需填，未提及的字段返回 null）：
{
  "description": "事项描述（一句话）",
  "assignee_hint": "原文中的人名提示（@张三/我/小李/null）",
  "deadline_raw": "原文中的时间表达（周三前/明天/5月20日/null）",
  "priority": "high|normal|low",
  "confidence": 0.0-1.0
}

判定标准：
- 只抽具体可执行的待办（"周三前给我方案"），跳过抽象/已完成/咨询性陈述
- 优先级：明说"急/尽快/紧急"=high；"不急/有空"=low；其他=normal
- 不确定时返回空数组 []

不返回任何解释文字，只返回 JSON 数组。
"""

_LLM_MESSAGE_CEILING = 200  # 安全上限，超过截断保尾


def _serialize(messages: list[dict]) -> str:
    """把 GroupMessage 列表渲染为 LLM 输入字符串。"""
    lines: list[str] = []
    for m in messages:
        ts = m.get("timestamp")
        ts_str = ts.strftime("%m-%d %H:%M") if isinstance(ts, datetime) else str(ts)
        sender = m.get("sender") or m.get("sender_id") or "?"
        content = (m.get("content") or "").strip().replace("\n", " ")
        if content:
            lines.append(f"[{ts_str}] {sender}: {content}")
    return "\n".join(lines)


def _extract_json_array(raw: str) -> list[dict] | None:
    """容错抽 JSON 数组：直接 / 代码块 / 文本中的 [...]"""
    if not raw:
        return None
    text = raw.strip()
    try:
        d = json.loads(text)
        if isinstance(d, list):
            return d
        if isinstance(d, dict) and "tasks" in d and isinstance(d["tasks"], list):
            return d["tasks"]
    except Exception:
        pass
    # code fence
    m = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    # bare [...]
    m = re.search(r"\[.*\]", text, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return None


def _coerce_priority(value: Any) -> str:
    if not value:
        return "normal"
    s = str(value).strip().lower()
    if s in ("high", "h", "高", "紧急", "急", "important"):
        return "high"
    if s in ("low", "l", "低", "不急"):
        return "low"
    return "normal"


def _coerce_confidence(value: Any) -> float:
    try:
        f = float(value)
        return max(0.0, min(1.0, f))
    except (TypeError, ValueError):
        return 0.5


def _resolve_assignee(
    hint: str | None, user_directory: dict[str, str] | None
) -> str | None:
    """从 hint 解析到 user_id。"""
    if not hint or not user_directory:
        return None
    h = hint.strip().lstrip("@").lower()
    if not h:
        return None
    # 精确匹配 / 模糊包含
    for name, uid in user_directory.items():
        n = name.strip().lower()
        if n == h or h in n or n in h:
            return uid
    return None


async def extract_tasks_from_messages(
    messages: list[dict],
    *,
    llm_provider: Provider,
    time_now: datetime,
    user_directory: dict[str, str] | None = None,
) -> list[ExtractedTask]:
    """主入口。空输入 / LLM 失败 / 解析失败均返回空列表。"""
    if not messages:
        return []

    trimmed = (
        messages[-_LLM_MESSAGE_CEILING:]
        if len(messages) > _LLM_MESSAGE_CEILING
        else list(messages)
    )
    prompt = _serialize(trimmed)
    if not prompt:
        return []

    try:
        resp = await llm_provider.text_chat(
            prompt=prompt,
            system_prompt=TASK_EXTRACTOR_SYSTEM_PROMPT,
        )
        raw = (
            getattr(resp, "completion_text", "")
            or getattr(resp, "raw_response", "")
            or ""
        )
    except Exception:
        return []

    parsed = _extract_json_array(raw)
    if not parsed:
        return []

    out: list[ExtractedTask] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        desc = str(item.get("description", "")).strip()
        if not desc:
            continue
        deadline_raw = item.get("deadline_raw")
        if deadline_raw in (None, "null", ""):
            deadline_raw = None
        out.append(
            ExtractedTask(
                description=desc,
                priority=_coerce_priority(item.get("priority")),
                confidence=_coerce_confidence(item.get("confidence")),
                assignee_hint=str(item.get("assignee_hint") or "").strip() or None,
                assignee_user_id=_resolve_assignee(
                    item.get("assignee_hint"), user_directory
                ),
                deadline_raw=deadline_raw,
                deadline=parse_deadline(deadline_raw, now=time_now),
                extras={
                    k: v
                    for k, v in item.items()
                    if k
                    not in (
                        "description",
                        "priority",
                        "confidence",
                        "assignee_hint",
                        "deadline_raw",
                    )
                },
            )
        )
    return out
