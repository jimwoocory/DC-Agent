"""Card action handler — disabled legacy CLI cards + governed-memory cards.

统一处理 ``event.message_str.startswith("__card_action__:")`` 路径：
1. 先吞掉旧 CLI 排队卡（只取消/关闭，不再执行）
2. 再尝试 department_memory_prompt 卡片（confirm / dismiss）
3. 再尝试 sop_signal_confirmation 卡片（remember / once / dismiss）
3. 都不是 → 返回 False 让 dispatch 让其他 plugin 接管
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Final

from astrbot.api import logger
from astrbot.api.event import MessageEventResult

CARD_ACTION_PREFIX: Final[str] = "__card_action__:"

# 已知 source 名 (跟 department_memory.build_department_memory_prompt_card 对齐)
DEPT_MEMORY_SOURCE: Final[str] = "department_memory_prompt"
SOP_SIGNAL_SOURCE: Final[str] = "sop_signal_confirmation"


@dataclass(slots=True)
class CardActionResult:
    handled: bool
    """True ⇒ dispatch 应当 return；False ⇒ 让其他 plugin 接管。"""
    stop: bool = False
    """True ⇒ event.should_call_llm(False) + stop_event (不进入 LLM 路由)。"""
    resumed_text: str = ""
    """当 card_action 把消息恢复为原文本 (dept memory confirm/dismiss) 时填充。"""


def _is_trusted(event: Any) -> bool:
    msg = getattr(event, "message_obj", None)
    return (
        getattr(event, "is_card_action", False) is True
        or getattr(msg, "is_card_action", False) is True
    )


def _parse_payload(event: Any) -> dict[str, Any]:
    payload = getattr(getattr(event, "message_obj", None), "card_action_payload", None)
    if isinstance(payload, dict):
        return payload
    text = (getattr(event, "message_str", "") or "").strip()
    if not text.startswith(CARD_ACTION_PREFIX):
        return {}
    try:
        parsed = json.loads(text[len(CARD_ACTION_PREFIX) :])
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _dept_memory_value(event: Any) -> dict[str, Any]:
    value = _card_value(event)
    if value.get("source") != DEPT_MEMORY_SOURCE:
        return {}
    return value


def _sop_signal_value(event: Any) -> dict[str, Any]:
    value = _card_value(event)
    if value.get("source") != SOP_SIGNAL_SOURCE:
        return {}
    return value


def _card_value(event: Any) -> dict[str, Any]:
    payload = _parse_payload(event)
    value = payload.get("value", {}) if isinstance(payload, dict) else {}
    if not isinstance(value, dict):
        return {}
    return value


async def try_handle_card_action(
    context: Any,
    event: Any,
) -> CardActionResult:
    """返回 handled=True 时 dispatch 应直接 return；其他 plugin 也会被跳过。"""
    text = (getattr(event, "message_str", "") or "").strip()
    if not text.startswith(CARD_ACTION_PREFIX):
        return CardActionResult(handled=False)

    # 1) 部门记忆卡片 (confirm / dismiss) — dispatch 阶段会再处理 dept memory，
    #    但这里先把「非信任 / 不匹配 pending」的情况显式吞掉避免泄漏到 LLM。
    dept_value = _dept_memory_value(event)
    if dept_value and not _is_trusted(event):
        try:
            event.should_call_llm(False)
            event.set_result(
                MessageEventResult().message("").use_t2i(False).stop_event()
            )
        except Exception:  # noqa: BLE001
            pass
        return CardActionResult(handled=True, stop=True)
    if dept_value:
        # 把卡片回调转成「恢复 pending 决策」，交给 dispatch.dept_memory 阶段处理
        return CardActionResult(handled=False, resumed_text=text)

    # 2) 员工处理习惯确认卡 — 信任校验后在这里完成 need_review 记忆导出
    sop_value = _sop_signal_value(event)
    if sop_value and not _is_trusted(event):
        try:
            event.should_call_llm(False)
            event.set_result(
                MessageEventResult().message("").use_t2i(False).stop_event()
            )
        except Exception:  # noqa: BLE001
            pass
        return CardActionResult(handled=True, stop=True)
    if sop_value:
        try:
            from .sop_signal import try_handle_sop_signal_card_action

            result = try_handle_sop_signal_card_action(event, sop_value)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[dc_router] sop signal card action failed: %s", exc)
            try:
                event.should_call_llm(False)
                event.set_result(
                    MessageEventResult().message("").use_t2i(False).stop_event()
                )
            except Exception:  # noqa: BLE001
                pass
            return CardActionResult(handled=True, stop=True)
        return CardActionResult(handled=result.handled, stop=result.stop)

    # 3) Disabled legacy CLI queue card
    try:
        from ..cli_handlers import handle_disabled_legacy_cli_card_action
    except Exception as exc:  # noqa: BLE001
        logger.warning("[dc_router] import legacy CLI card handler failed: %s", exc)
        return CardActionResult(handled=False)

    try:
        handled = await handle_disabled_legacy_cli_card_action(context, event)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[dc_router] 排队卡按钮处理失败: %s", exc)
        # 异常时直接退出 — 避免把异常状态带入下游 LLM 路由
        try:
            event.should_call_llm(False)
            event.set_result(
                MessageEventResult().message("").use_t2i(False).stop_event()
            )
        except Exception:  # noqa: BLE001
            pass
        return CardActionResult(handled=True, stop=True)

    if handled:
        return CardActionResult(handled=True, stop=True)

    # 非 router 卡片 (pet / 自定义卡) — 让其他 plugin 接管，绝不让卡片回调
    # 进入 LLM 路由
    logger.debug(
        "[dc_router] 非 router 卡片回调，让其他 plugin 接管: %s",
        text[:80],
    )
    return CardActionResult(handled=True, stop=False)


__all__ = ["CardActionResult", "try_handle_card_action"]
