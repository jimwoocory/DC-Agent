"""Health subsystem — circuit-breaker state for at-risk providers.

任何路由决策应该先看 provider health circuit
状态（已由 routing.apply_decision 之前的 hooks 处理）；这里只暴露统一
的 health-snapshot 接口供 dashboard / cli 使用。
"""

from __future__ import annotations

from typing import Any

from .qwen_health import (
    get_qwen_health,
    mark_qwen3_6_flash_failure,
    mark_qwen3_6_flash_success,
    qwen3_6_flash_allowed,
    reset_qwen_health,
    summarize_qwen_history,
)


def health_snapshot() -> dict[str, Any]:
    """Return the current health for all routed providers — for dashboard / CLI."""
    qwen = get_qwen_health()
    return {
        "qwen3.6_flash": {
            "provider_id": qwen.get("provider_id", ""),
            "available": bool(qwen.get("available", True)),
            "status": qwen.get("status", ""),
            "reason": qwen.get("reason", ""),
            "remaining_seconds": int(qwen.get("remaining_seconds") or 0),
            "consecutive_failures": int(qwen.get("consecutive_failures") or 0),
            "last_error_code": qwen.get("last_error_code", ""),
        },
    }


__all__ = [
    "get_qwen_health",
    "health_snapshot",
    "mark_qwen3_6_flash_failure",
    "mark_qwen3_6_flash_success",
    "qwen3_6_flash_allowed",
    "reset_qwen_health",
    "summarize_qwen_history",
]
