"""Host-agnostic Feishu card builders used by router-side prompts."""

from __future__ import annotations

from typing import Any

from .spiral_evolution import build_low_friction_sop_confirmation


def _button(text: str, value: dict[str, Any], button_type: str = "default") -> dict:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": text},
        "type": button_type,
        "value": value,
    }


def build_department_memory_prompt_card(state: Any) -> dict[str, Any]:
    names = "、".join(getattr(state, "department_names", []) or []) or "相关部门"
    suggestion_id = str(getattr(state, "suggestion_id", "") or "")
    base = {"source": "department_memory_prompt", "suggestion_id": suggestion_id}
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": "是否调用部门记忆"},
        },
        "elements": [
            {
                "tag": "markdown",
                "content": (
                    f"检测到这像 **{names}** 相关任务。\n"
                    "可以调用已通过 Obsidian 审核的部门记忆辅助回答。"
                ),
            },
            {
                "tag": "markdown",
                "content": (
                    "调用后只作为低优先级参考，不覆盖你本轮明确要求和已提供资料。"
                ),
            },
            {
                "tag": "action",
                "actions": [
                    _button("调用记忆", {**base, "action": "confirm"}, "primary"),
                    _button("不用", {**base, "action": "dismiss"}),
                ],
            },
        ],
    }


def build_sop_signal_confirmation_card(state: Any) -> dict[str, Any]:
    signal = getattr(state, "signal", {}) or {}
    signal_id = str(getattr(state, "signal_id", "") or "")
    confirmation = build_low_friction_sop_confirmation(signal)
    base = {"source": "sop_signal_confirmation", "signal_id": signal_id}
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {
                "tag": "plain_text",
                "content": str(confirmation["title"]),
            },
        },
        "elements": [
            {
                "tag": "markdown",
                "content": str(confirmation["message"]),
            },
            {
                "tag": "action",
                "actions": [
                    _button("记住", {**base, "action": "remember"}, "primary"),
                    _button("只这次", {**base, "action": "once"}),
                    _button("不用", {**base, "action": "dismiss"}),
                ],
            },
        ],
    }
