"""Chitchat guard: short greetings / thanks / farewells bypass LLM routing.

阈值 / 限速 / 日志路径都是模块级常量；handler 函数无副作用 (除了 set_result
+ should_call_llm(False) + event log)。
"""

from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from astrbot.api import logger
from astrbot.api.event import MessageEventResult

from ..paths import data_path

UTC: Final = timezone.utc

_CHITCHAT_MAX_LEN: Final[int] = 8
_CHITCHAT_RATE_LIMIT_SEC: Final[float] = 3.0
_CHITCHAT_MISS_LOG_PATH: Final[Path] = data_path("chitchat_guard_misses.jsonl")
_CHITCHAT_HIT_LOG_PATH: Final[Path] = data_path("chitchat_guard_hits.jsonl")

_CHITCHAT_PUNCT_RE: Final[re.Pattern] = re.compile(
    r"[\s，。！？、~～?!\.,;；:：\"'“”‘’（）()【】\[\]{}<>《》]+"
)
_CHITCHAT_AT_RE: Final[re.Pattern] = re.compile(r"^\s*(?:\[At:[^\]]+\]|@[^\s]+\s*)+")
_CHITCHAT_NEGATIVE_RE: Final[re.Pattern] = re.compile(
    r"(查|调|写|改|跑|算|搜|找|做|生成|优化|报错|错误|bug|任务|待办|提醒|方案|项目|资料|文件|链接|推文|群)"
)

# 命中词库 — 关键词级别（v1 同等强度）
_CHITCHAT_RESPONSES: Final[dict[str, dict[str, tuple[str, ...]]]] = {
    "greeting": {
        "keywords": (
            "你好",
            "您好",
            "hello",
            "hi",
            "hey",
            "你好呀",
            "在吗",
            "在不",
            "在",
            "在？",
            "喂",
        ),
        "responses": (
            "您好，我在的。您需要我协助处理什么内容，直接发我就好。",
            "在的，您可以直接把需要我协助的内容发给我。",
            "您好，我在。需要我帮您看资料、整理内容或处理问题，都可以直接发我。",
        ),
    },
    "thanks": {
        "keywords": (
            "谢谢",
            "感谢",
            "谢了",
            "太感谢了",
            "辛苦了",
            "麻烦你了",
            "thanks",
            "thx",
        ),
        "responses": (
            "不客气，后续有需要您随时找我。",
            "不辛苦，能帮上忙就好。您后面有需要可以继续发我。",
            "收到，后续需要我继续协助的话，您直接说就好。",
        ),
    },
    "farewell": {
        "keywords": ("再见", "拜拜", "bye", "goodbye"),
        "responses": (
            "好的，后续有需要您随时找我。",
            "再见，祝您工作顺利。",
        ),
    },
    "identity": {
        "keywords": ("你是谁", "你叫啥", "你是啥", "你是什么", "你叫什么"),
        "responses": (
            "我是巅池-Agent 小助手，可以协助您整理资料、优化内容、查询项目和处理日常协作问题。",
            "我是巅池-Agent 小助手，主要协助大家做资料整理、内容优化、项目查询和工作协同。",
        ),
    },
}

_CHITCHAT_LAST_HIT: dict[str, float] = {}


@dataclass(slots=True)
class ChitchatResult:
    handled: bool
    response: str = ""
    matched_intent: str = ""


def _normalize(text: str) -> str:
    cleaned = _CHITCHAT_AT_RE.sub("", text or "")
    return _CHITCHAT_PUNCT_RE.sub("", cleaned.strip().lower())


def _chitchat_response_for(text: str) -> tuple[str, str] | None:
    """Return (matched_intent, response) if the text matches a chitchat intent."""
    normalized = _normalize(text)
    if not normalized or len(normalized) > _CHITCHAT_MAX_LEN:
        return None
    for intent, data in _CHITCHAT_RESPONSES.items():
        if normalized in data["keywords"]:
            return intent, random.choice(data["responses"])
    return None


def _should_record_miss(text: str) -> bool:
    normalized = _normalize(text)
    if not normalized or len(normalized) > _CHITCHAT_MAX_LEN:
        return False
    return not _CHITCHAT_NEGATIVE_RE.search(normalized)


def _is_group_event(event: Any) -> bool:
    umo = str(getattr(event, "unified_msg_origin", "") or "")
    if "GroupMessage" in umo or ":group:" in umo.lower():
        return True
    try:
        return bool(event.get_group_id())
    except Exception:  # noqa: BLE001
        return False


def _sender_rate_key(event: Any) -> str:
    try:
        sender_id = str(event.get_sender_id() or "")
    except Exception:  # noqa: BLE001
        sender_id = ""
    return f"{event.unified_msg_origin or ''}:{sender_id}"


def _append_log(path: Path, event: Any, text: str) -> None:
    try:
        sender_id = str(event.get_sender_id() or "")
    except Exception:  # noqa: BLE001
        sender_id = ""
    payload = {
        "created_at": datetime.now(UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "platform_id": event.get_platform_id() or "",
        "session_id": event.unified_msg_origin or "",
        "sender_id": sender_id,
        "raw_text": (text or "").strip()[:80],
        "normalized_text": _normalize(text),
        "source": "dc_router_chitchat_guard",
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception as exc:  # noqa: BLE001
        logger.debug("[dc_router] chitchat guard log skipped: %s", exc)


async def try_handle_chitchat(event: Any, text: str) -> ChitchatResult:
    """Return handled=True 时表示 dispatch 应当停止后续处理。

    只在 group event + 不是 at/wake 命令时直接放行；
    其余场景根据 normalized text 匹配 hit/miss。
    """
    if _is_group_event(event) and not getattr(event, "is_at_or_wake_command", False):
        return ChitchatResult(handled=False)

    matched = _chitchat_response_for(text)
    if matched is None:
        if _should_record_miss(text):
            _append_log(_CHITCHAT_MISS_LOG_PATH, event, text)
        return ChitchatResult(handled=False)

    intent, response = matched
    now = time.monotonic()
    rate_key = _sender_rate_key(event)
    last_hit = _CHITCHAT_LAST_HIT.get(rate_key, 0.0)
    _CHITCHAT_LAST_HIT[rate_key] = now
    if now - last_hit < _CHITCHAT_RATE_LIMIT_SEC:
        response = "我在的，您可以把需要我协助的内容一次发完整，我会尽快处理。"

    _append_log(_CHITCHAT_HIT_LOG_PATH, event, text)
    try:
        event.should_call_llm(False)
        event.set_result(
            MessageEventResult().message(response).use_t2i(False).stop_event()
        )
    except Exception:  # noqa: BLE001
        return ChitchatResult(handled=False)

    logger.info(
        "[dc_router] chitchat guard hit platform=%s intent=%s session=%s",
        event.get_platform_id() or "",
        intent,
        event.unified_msg_origin,
    )
    return ChitchatResult(handled=True, response=response, matched_intent=intent)


__all__ = ["ChitchatResult", "try_handle_chitchat"]
