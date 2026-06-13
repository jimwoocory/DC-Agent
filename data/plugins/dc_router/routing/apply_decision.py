"""Apply a RouterDecision to an AstrMessageEvent.

Decoupling: 这个模块是唯一允许调 ``context.set_provider`` + ``event.set_extra``
的边界层。所有 routing 决策 (business / ops) 都收敛到 ``apply_decision``。

任何异常都被吞掉并返回 False — 调用方根据 R5 fallback_on_error 决定是否回退 v1.0。

CLI 路径 (``cli/...`` provider_id) 在这里统一分发到
``cli_handlers.dispatch_cli_provider``, 集中处理 antigravity / codex / grok
的 QuotaGate / circuit breaker / 卡片渲染 / fallback 逻辑.
"""

from __future__ import annotations

import logging
from typing import Any

from astrbot.api import logger

# Provider → reasoning tier 的派生（用于下游 daily_card_renderer 决定动画/配额）
_TIER_MARKERS: tuple[tuple[str, str], ...] = (
    ("xhigh", "xhigh"),
    ("high", "high"),
    ("medium", "medium"),
)


def _derive_tier(provider_id: str) -> str:
    lowered = (provider_id or "").lower()
    for marker, tier in _TIER_MARKERS:
        if marker in lowered:
            return tier
    return ""


def _resolve_provider(context: Any, provider_id: str) -> Any:
    getter = getattr(context, "get_provider_by_id", None)
    if not callable(getter):
        return None
    try:
        return getter(provider_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[dc_router] get_provider_by_id(%s) 失败: %s", provider_id, exc)
        return None


async def _set_provider(
    context: Any,
    umo: str,
    provider_id: str,
) -> bool:
    if not provider_id or not umo:
        return False
    try:
        # 延迟 import, 避免非 AstrBot 环境下 import 失败
        from astrbot.core.provider.entities import ProviderType

        await context.provider_manager.set_provider(
            provider_id=provider_id,
            provider_type=ProviderType.CHAT_COMPLETION,
            umo=umo,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[dc_router] set_provider 失败 (%s): %s",
            provider_id,
            exc,
        )
        return False
    return True


def _annotate_event(
    event: Any,
    *,
    provider_id: str,
    intent: str | None,
    source: str,
    reason: str = "",
    metadata: dict[str, Any] | None = None,
) -> None:
    """把决策写到 event.set_extra；不修改 event.message_str（避免污染用户可见输入）.

    下游 daily_card_renderer / harness_state_injector 等靠 extras 读决策结果。
    """
    try:
        tier = _derive_tier(provider_id)
        if tier:
            event.set_extra("reasoning_tier", tier)
        event.set_extra("dc_router_intent", intent or "")
        event.set_extra("dc_router_source", source)
        event.set_extra("dc_router_provider", provider_id)
        if reason:
            event.set_extra("dc_router_reason", reason)
        if metadata:
            for key, value in metadata.items():
                if value is not None:
                    event.set_extra(f"dc_router_meta_{key}", str(value))
    except Exception:  # noqa: BLE001
        # event 在某些测试夹具里没有 set_extra — 静默忽略
        logging.getLogger(__name__).debug("annotate_event failed", exc_info=True)


async def apply_provider_pin(
    context: Any,
    event: Any,
    *,
    target_provider_id: str,
    source: str,
    intent: str | None = None,
    reason: str = "",
) -> bool:
    """Pin a specific provider (used by reasoning prefix / feishu channel / v1.0)."""
    if not target_provider_id:
        return False
    if _resolve_provider(context, target_provider_id) is None:
        logger.warning(
            "[dc_router] provider %s 不在 available 列表，跳过",
            target_provider_id,
        )
        return False
    umo = getattr(event, "unified_msg_origin", "") or ""
    if not await _set_provider(context, umo, target_provider_id):
        return False
    _annotate_event(
        event,
        provider_id=target_provider_id,
        intent=intent,
        source=source,
        reason=reason,
    )
    logger.info(
        "[dc_router] %s · platform=%s · intent=%s · provider=%s · tier=%s · '%s'",
        source,
        _safe_platform(event),
        intent or "-",
        target_provider_id,
        _derive_tier(target_provider_id) or "-",
        _safe_text(event)[:30].replace("\n", " "),
    )
    return True


async def apply_decision(
    context: Any,
    event: Any,
    decision: Any,
) -> bool:
    """把 RouterDecision 应用到 event + provider.

    返回 True 表示已成功接管（含 set_provider / CLI handler 已执行）。
    返回 False 时由 dispatch.py 决定是否 fallback 到 v1.0。
    """
    provider_id = str(getattr(decision, "provider_id", "") or "")
    if not provider_id:
        logger.debug("[dc_router] RouterDecision.provider_id 为空，跳过 apply")
        return False
    intent = str(getattr(decision, "intent", "") or "")
    source = str(getattr(decision, "source", "") or "rules")
    reason = str(getattr(decision, "reason", "") or "")
    metadata = getattr(decision, "metadata", None)

    # CLI 路径: 委托给 cli_handlers (antigravity / codex / grok)
    if provider_id.startswith("cli/"):
        try:
            from ..cli_handlers import dispatch_cli_provider

            handled = await dispatch_cli_provider(context, event, decision)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[dc_router] dispatch_cli_provider(%s) 异常: %s",
                provider_id,
                exc,
            )
            handled = False
        if handled:
            _annotate_event(
                event,
                provider_id=provider_id,
                intent=intent,
                source=f"cli_{source}",
                reason=reason,
                metadata=metadata if isinstance(metadata, dict) else None,
            )
        return handled

    # 常规 API provider: set_provider 即可
    if _resolve_provider(context, provider_id) is None:
        logger.warning(
            "[dc_router] RouterDecision.provider_id=%s 不可用，apply 失败",
            provider_id,
        )
        return False
    umo = getattr(event, "unified_msg_origin", "") or ""
    if not await _set_provider(context, umo, provider_id):
        return False
    _annotate_event(
        event,
        provider_id=provider_id,
        intent=intent,
        source=source,
        reason=reason,
        metadata=metadata if isinstance(metadata, dict) else None,
    )
    logger.info(
        "[dc_router] decision · platform=%s · intent=%s · provider=%s · "
        "tier=%s · source=%s · '%s'",
        _safe_platform(event),
        intent or "-",
        provider_id,
        _derive_tier(provider_id) or "-",
        source or "-",
        _safe_text(event)[:30].replace("\n", " "),
    )
    return True


def annotate_event_with_decision(
    event: Any,
    decision: Any,
) -> None:
    """Expose a decision to downstream readers without touching provider state.

    Used in dry-run mode — we want the logs and extras to reflect what dc-router
    *would* have done, but we still want v1.0 to actually answer.
    """
    provider_id = str(getattr(decision, "provider_id", "") or "")
    intent = str(getattr(decision, "intent", "") or "")
    source = str(getattr(decision, "source", "") or "rules")
    reason = str(getattr(decision, "reason", "") or "")
    metadata = getattr(decision, "metadata", None)
    _annotate_event(
        event,
        provider_id=provider_id,
        intent=intent,
        source=f"dry_run_{source}",
        reason=reason,
        metadata=metadata if isinstance(metadata, dict) else None,
    )
    logger.info(
        "[dc_router · DRY-RUN] decision · platform=%s · intent=%s · provider=%s · '%s'",
        _safe_platform(event),
        intent or "-",
        provider_id or "-",
        _safe_text(event)[:30].replace("\n", " "),
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


def _safe_text(event: Any) -> str:
    try:
        return str(event.message_str or "")
    except Exception:  # noqa: BLE001
        return ""


__all__ = [
    "annotate_event_with_decision",
    "apply_decision",
    "apply_provider_pin",
]
