"""Department memory activation prompt.

v1.0 行为:
- 用户发了请求 → 匹配 dept profile (matching_department_memory_profiles)
- 命中 *且* 有 approved 记忆 → 给用户发「是否调用记忆」卡片 / fallback 文字
- 用户回复「调用记忆」/「不用」/ 卡片 confirm/dismiss → inject / skip

实现: 单个 dataclass 表达决策，dispatch 阶段根据 decision 决定:
- stop: send card / fallback text + return
- inject: inject_memory_context_into_event (which uses set_extra only)
- dismissed: 继续走 (event.message_str 恢复原文本)
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from astrbot.api import logger

from ..paths import data_path

UTC: Final = timezone.utc

_AUDIT_PATH: Final[Path] = data_path("department_memory_prompt_audit.jsonl")
_PROMPT_TTL_SEC: Final[float] = 600.0

_CONFIRM_RE: Final[re.Pattern] = re.compile(
    r"^\s*(调用记忆|带上记忆|使用记忆|用记忆|确认调用|确认|可以|好的|好|是|yes|y|ok)\s*[。！!,.，]*\s*$",
    re.IGNORECASE,
)
_DISMISS_RE: Final[re.Pattern] = re.compile(
    r"^\s*(不用|不调用|不用记忆|先不用|不要|取消|否|no|n)\s*[。！!,.，]*\s*$",
    re.IGNORECASE,
)
_EXPLICIT_MEMORY_LOOKUP_RE: Final[re.Pattern] = re.compile(
    r"(记忆|历史|之前|上次|过往|沉淀|查一下|找一下|负责人|资料|文件|来源|引用|"
    r"知识库|公司库|NAS|Obsidian|沿用|复用)",
    re.IGNORECASE,
)
_VISUAL_MEDIA_REQUEST_RE: Final[re.Pattern] = re.compile(
    r"(\[image\]|这张图|那张图|这张图片|那张图片|图片|图像|截图|照片|原图|"
    r"主图|页面图|店铺图|海报|封面|视觉|素材图|生图|文生图|画图|改图|修图|抠图|"
    r"生成.{0,12}图|设计.{0,12}图|做.{0,12}图)",
    re.IGNORECASE,
)
_METACONV_PATTERNS: Final[tuple[re.Pattern, ...]] = (
    re.compile(r"明白了吗|懂了吗|知道了吗|我说什么", re.IGNORECASE),
    re.compile(r"压力测试|stress.test|在测试你|测试一下", re.IGNORECASE),
    re.compile(r"我.*就是.*部门|其实我是.*部", re.IGNORECASE),
    re.compile(r"不是.*在测试吗|就是.*压力测试", re.IGNORECASE),
    re.compile(r"^\s*(是|对|嗯|是的|没错)\s*[。!！]?\s*$"),
)
# 部门记忆回调时 (true) 跳过 suggest
_TRUSTED_CARD_SOURCE: Final[str] = "department_memory_prompt"
_IDENTITY_OVERRIDES_PATH: Final[Path] = data_path(
    "config", "employee_identity_overrides.json"
)


def _short_id(value: str) -> str:
    if not value:
        return ""
    return value if len(value) <= 16 else f"{value[:8]}…{value[-6:]}"


def _read_event_attr(obj: Any, attr_path: tuple[str, ...]) -> Any:
    cur = obj
    for attr in attr_path:
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(attr)
        else:
            cur = getattr(cur, attr, None)
    return cur


def _candidate_event_ids(event: Any) -> dict[str, str]:
    try:
        sender_id = str(event.get_sender_id() or "")
    except Exception:  # noqa: BLE001
        sender_id = ""
    message_obj = getattr(event, "message_obj", None)
    raw_message = getattr(message_obj, "raw_message", None)
    candidates = {
        "sender_id": sender_id,
        "origin": str(getattr(event, "unified_msg_origin", "") or ""),
        "message_sender_user_id": str(
            _read_event_attr(message_obj, ("sender", "user_id")) or ""
        ),
        "message_sender_open_id": str(
            _read_event_attr(message_obj, ("sender", "open_id")) or ""
        ),
        "raw_sender_id": str(
            _read_event_attr(raw_message, ("sender", "sender_id")) or ""
        ),
        "raw_sender_open_id": str(
            _read_event_attr(raw_message, ("sender", "sender_id", "open_id")) or ""
        ),
        "raw_chat_id": str(_read_event_attr(raw_message, ("chat_id",)) or ""),
        "raw_p2p_chat_id": str(_read_event_attr(raw_message, ("p2p_chat_id",)) or ""),
    }
    return {key: value for key, value in candidates.items() if value}


def _is_system_tester_event(event: Any) -> bool:
    """Return true for configured system testers.

    System testers intentionally switch department context during realistic QA. Do
    not interrupt their normal work requests with department-memory suggestion
    cards; inject available memory context and let routing continue instead.
    """
    candidates = _candidate_event_ids(event)
    try:
        raw = _IDENTITY_OVERRIDES_PATH.read_text(encoding="utf-8-sig")
        payload = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[dc_router] identity overrides unavailable for tester check: %s", exc
        )
        return False
    for employee in payload.get("employees") or []:
        if not isinstance(employee, dict) or not employee.get("is_system_tester"):
            continue
        open_id = str(employee.get("open_id") or "")
        chat_ids = {str(x) for x in employee.get("chat_ids") or [] if x}
        exact_ids = {
            value
            for value in candidates.values()
            if value and not value.startswith("aiocqhttp:")
        }
        origin = candidates.get("origin", "")
        if open_id and (open_id in exact_ids or open_id in origin):
            logger.info(
                "[dc_router] system tester matched by open_id open_id=%s candidates=%s",
                _short_id(open_id),
                {key: _short_id(value) for key, value in candidates.items()},
            )
            return True
        matched_chat_id = next(
            (
                chat_id
                for chat_id in chat_ids
                if chat_id in exact_ids or chat_id in origin
            ),
            "",
        )
        if matched_chat_id:
            logger.info(
                "[dc_router] system tester matched by chat_id chat_id=%s candidates=%s",
                _short_id(matched_chat_id),
                {key: _short_id(value) for key, value in candidates.items()},
            )
            return True
    logger.debug(
        "[dc_router] system tester not matched candidates=%s",
        {key: _short_id(value) for key, value in candidates.items()},
    )
    return False


@dataclass(slots=True)
class DepartmentMemoryPromptState:
    suggestion_id: str
    conversation_id: str
    original_text: str
    query_text: str
    department_ids: tuple[str, ...]
    department_names: tuple[str, ...]
    profile_ids: tuple[str, ...]
    created_at: float
    status: str = "suggested"


@dataclass(slots=True)
class DepartmentMemoryDecision:
    stop: bool = False
    inject_memory: bool = False
    dismissed: bool = False
    effective_text: str = ""
    memory_query_text: str = ""
    suggestion_id: str = ""
    audit_state: DepartmentMemoryPromptState | None = None
    pending_state_to_store: DepartmentMemoryPromptState | None = None


# Pending 提示是 session 级别 — module 级 dict 即可
_PENDING: dict[str, DepartmentMemoryPromptState] = {}


def _pending_key(event: Any) -> str:
    try:
        sender_id = str(event.get_sender_id() or "")
    except Exception:  # noqa: BLE001
        sender_id = ""
    return f"{getattr(event, 'unified_msg_origin', '') or ''}:{sender_id}"


def _is_meta_conversation(text: str) -> bool:
    t = text or ""
    return any(pat.search(t) for pat in _METACONV_PATTERNS)


def _should_skip_proactive_department_memory(text: str) -> bool:
    """Avoid interrupting low-context visual tasks with department memory.

    Image understanding, image generation, and image editing are driven by the
    current attachment and prompt. Department memory is still available through
    the explicit memory lookup branch that runs before this guard.
    """
    return bool(_VISUAL_MEDIA_REQUEST_RE.search(text or ""))


def _has_dominant_profile(profiles: list, text: str) -> bool:
    """True if the top profile has clearly higher score than the second.

    Scoring: alias match = 4, keyword match = 1. A gap of 3+ means alias
    vs keyword-level dominance.
    """
    if len(profiles) < 2:
        return True
    scores: list[int] = []
    for profile in profiles:
        score = 0
        for alias in getattr(profile, "aliases", None) or ():
            if alias and alias in (text or ""):
                score += 4
        for kw in getattr(profile, "trigger_keywords", None) or ():
            if kw and re.search(re.escape(kw), text or "", flags=re.IGNORECASE):
                score += 1
        scores.append(score)
    return scores[0] - scores[1] >= 3


def _has_approved_department_memory(query_text: str, profiles: list) -> bool:
    if not profiles:
        return False
    try:
        from ..memory_injection import retrieve_governed_memory_context

        context = retrieve_governed_memory_context(query_text, limit=8)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[dc_router] dept memory lookup skipped: %s", exc)
        return False
    memories = context.get("governed_memories") or []
    dept_ids = {getattr(p, "department_id", "") for p in profiles}
    names = {getattr(p, "display_name", "") for p in profiles}
    for mem in memories:
        if mem.get("review_status") != "approved":
            continue
        if mem.get("sensitivity") not in {"public", "internal"}:
            continue
        haystack = " ".join(
            str(x)
            for x in (
                mem.get("owner") or "",
                mem.get("project_id") or "",
                " ".join(str(t) for t in mem.get("tags") or []),
                mem.get("title") or "",
            )
        )
        if any(d and d in haystack for d in dept_ids):
            return True
        if any(n and n in haystack for n in names):
            return True
    return False


def _suggestion_id(session_key: str, text: str) -> str:
    seed = f"{session_key}:{text}:{int(time.time())}"
    return f"dmpp_{abs(hash(seed)):x}"[:20]


def _append_audit(
    *,
    action: str,
    state: DepartmentMemoryPromptState,
    status_before: str,
    status_after: str,
    payload: dict | None = None,
) -> None:
    record = {
        "actor": "dc_router",
        "action": action,
        "suggestion_id": state.suggestion_id,
        "memory_ids": [],
        "department_id": ",".join(state.department_ids),
        "conversation_id": state.conversation_id,
        "status_before": status_before,
        "status_after": status_after,
        "payload": payload or {},
        "timestamp": datetime.now(UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
    }
    try:
        _AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _AUDIT_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception as exc:  # noqa: BLE001
        logger.debug("[dc_router] dept memory audit skipped: %s", exc)


def _build_prompt_text(state: DepartmentMemoryPromptState) -> str:
    names = "、".join(state.department_names) or "相关部门"
    return (
        f"我检测到这像「{names}」相关任务。\n"
        "是否调用已通过 Obsidian 审核的部门记忆来辅助这次回答？\n"
        f"回复「调用记忆」我会带上；回复「不用」则不调用。本次建议 ID: {state.suggestion_id}"
    )


def prompt_text(state: DepartmentMemoryPromptState) -> str:
    """导出 — dispatch / card 渲染都能复用同一文案。"""
    return _build_prompt_text(state)


def try_handle_department_memory(
    event: Any,
    *,
    raw_text: str,
    query_text: str,
    send_prompt_response: bool = True,
) -> DepartmentMemoryDecision:
    """Stateful: 需要先看 pending 状态。

    三个分支:
    1) 用户回复「调用记忆」/「不用」 → resolve pending → 后续 inject / dismiss
    2) 用户 *是* card callback 且 suggestion_id 匹配 → 同上 (走 confirm/dismiss)
    3) 新请求 → 匹配 profile → 命中则给提示
    """
    text = (raw_text or "").strip()
    session_key = _pending_key(event)

    # 1) 显式确认 / dismiss 走 text-based 分支
    pending = _PENDING.get(session_key)
    if pending is not None:
        age = time.monotonic() - pending.created_at
        if age > _PROMPT_TTL_SEC:
            _PENDING.pop(session_key, None)
            _append_audit(
                action="expire",
                state=pending,
                status_before=pending.status,
                status_after="expired",
                payload={"age_sec": round(age, 3)},
            )
            return DepartmentMemoryDecision()
        if _CONFIRM_RE.match(text):
            _PENDING.pop(session_key, None)
            confirmed = DepartmentMemoryPromptState(
                suggestion_id=pending.suggestion_id,
                conversation_id=pending.conversation_id,
                original_text=pending.original_text,
                query_text=pending.query_text,
                department_ids=pending.department_ids,
                department_names=pending.department_names,
                profile_ids=pending.profile_ids,
                created_at=pending.created_at,
                status="confirmed",
            )
            _append_audit(
                action="confirm",
                state=confirmed,
                status_before=pending.status,
                status_after="confirmed",
            )
            return DepartmentMemoryDecision(
                inject_memory=True,
                effective_text=pending.original_text,
                memory_query_text=pending.query_text,
                suggestion_id=pending.suggestion_id,
                audit_state=confirmed,
            )
        if _DISMISS_RE.match(text):
            _PENDING.pop(session_key, None)
            _append_audit(
                action="dismiss",
                state=pending,
                status_before=pending.status,
                status_after="dismissed",
            )
            return DepartmentMemoryDecision(
                dismissed=True,
                effective_text=pending.original_text,
                memory_query_text=pending.query_text,
                suggestion_id=pending.suggestion_id,
            )

    # 2) 显式记忆查询 — 直接 inject。系统测试身份不能改变业务语义。
    if _EXPLICIT_MEMORY_LOOKUP_RE.search(text):
        return DepartmentMemoryDecision(
            inject_memory=True,
            effective_text=text,
            memory_query_text=query_text,
        )

    # 3) 压力测试 / 元对话 — 不过滤 dept memory 建议
    if _is_meta_conversation(text):
        return DepartmentMemoryDecision(
            inject_memory=True,
            effective_text=text,
            memory_query_text=query_text,
        )

    # 4) 图片理解 / 生图 / 改图默认依赖当前图和当前指令，不弹部门记忆卡。
    if _should_skip_proactive_department_memory(text):
        logger.info(
            "[dc_router] dept memory prompt skipped for visual media request platform=%s",
            _safe_platform(event),
        )
        return DepartmentMemoryDecision(effective_text=text)

    # 4) 找 profile — 找到多个且不够 dominant 时不打扰用户
    try:
        from dc_engines.department_workflows.memory_profiles import (
            matching_department_memory_profiles,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[dc_router] memory_profiles import failed: %s", exc)
        return DepartmentMemoryDecision(
            inject_memory=True,
            effective_text=text,
            memory_query_text=query_text,
        )

    profiles = matching_department_memory_profiles(text, limit=3)
    if not profiles:
        return DepartmentMemoryDecision(
            inject_memory=True,
            effective_text=text,
            memory_query_text=query_text,
        )
    if len(profiles) > 1 and not _has_dominant_profile(profiles, text):
        return DepartmentMemoryDecision(
            inject_memory=True,
            effective_text=text,
            memory_query_text=query_text,
        )
    if not _has_approved_department_memory(query_text, profiles):
        return DepartmentMemoryDecision(effective_text=text)

    state = DepartmentMemoryPromptState(
        suggestion_id=_suggestion_id(session_key, text),
        conversation_id=session_key,
        original_text=text,
        query_text=query_text,
        department_ids=tuple(p.department_id for p in profiles),
        department_names=tuple(p.display_name for p in profiles),
        profile_ids=tuple(p.profile_id for p in profiles),
        created_at=time.monotonic(),
    )
    _PENDING[session_key] = state
    _append_audit(
        action="suggest",
        state=state,
        status_before="",
        status_after="suggested",
        payload={"profile_ids": list(state.profile_ids)},
    )
    logger.info(
        "[dc_router] dept memory prompt suggested platform=%s departments=%s",
        _safe_platform(event),
        ",".join(state.department_names),
    )
    if not send_prompt_response:
        return DepartmentMemoryDecision(
            stop=True,
            effective_text=text,
            memory_query_text=query_text,
            suggestion_id=state.suggestion_id,
            audit_state=state,
            pending_state_to_store=state,
        )
    return DepartmentMemoryDecision(
        stop=True,
        effective_text=text,
        memory_query_text=query_text,
        suggestion_id=state.suggestion_id,
        audit_state=state,
        pending_state_to_store=state,
    )


def _safe_platform(event: Any) -> str:
    getter = getattr(event, "get_platform_id", None)
    if not callable(getter):
        return ""
    try:
        raw = getter()
    except Exception:  # noqa: BLE001
        return ""
    return str(raw or "")


__all__ = [
    "DepartmentMemoryDecision",
    "DepartmentMemoryPromptState",
    "prompt_text",
    "try_handle_department_memory",
]
