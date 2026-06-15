"""Pipeline orchestrator; single entry point used by the Star plugin.

Order matters:
  1) card action callbacks
  2) slash commands handled by AstrBot
  3) short acknowledgements and chitchat
  4) reasoning prefix provider pinning
  5) Feishu channel provider pinning
  6) truth intake material guard
  7) department memory prompt
  8) memory context injection via extras only
  9) assistant tone context via extras only
 10) media route background tasks
 11) dc_router.decide() for intent classification and provider routing
 12) v1.0 fallback when disabled, dry-run, or decide raises

Any stage that returns ``stop=True`` ends dispatch immediately.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from astrbot.api import logger

from .config import DCRouterConfig, is_dc_router_managed_platform, load_config
from .preprocessing import (
    try_apply_feishu_channel_route,
    try_handle_card_action,
    try_handle_chitchat,
    try_handle_department_memory,
    try_handle_media_route,
    try_inject_assistant_tone,
)
from .routing import (
    annotate_event_with_decision,
    apply_decision,
    apply_provider_pin,
    build_envelope,
    classify_intent_v1,
    match_reasoning_prefix,
    reason_with_llm_v1,
)

_SLASH_RE = re.compile(r"^\s*/\S+(?:\s|$)")


@dataclass(slots=True)
class DispatchResult:
    handled: bool
    """True ⇒ plugin.route() should return immediately."""
    source: str = ""
    """Stage that handled the message."""
    decision_provider: str = ""
    decision_intent: str = ""


async def _maybe_truth_intake(
    context: Any,
    event: Any,
    cfg: DCRouterConfig,
) -> bool:
    """Return True when truth intake already stopped the event."""
    if not (cfg.is_active or cfg.is_dry_run):
        return False
    try:
        from .truth_intake import maybe_handle_truth_intake
    except Exception as exc:  # noqa: BLE001
        logger.debug("[dc_router] truth_intake import failed: %s", exc)
        return False
    try:
        handled = await maybe_handle_truth_intake(context, event, _cfg_dict(cfg))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[dc_router] truth intake guard 失败，继续路由: %s", exc)
        return False
    return bool(handled)


async def _memory_injection(
    event: Any,
    *,
    query_text: str,
) -> bool:
    try:
        from .memory_injection import inject_memory_context_into_event
    except Exception as exc:  # noqa: BLE001
        logger.debug("[dc_router] memory_injection import failed: %s", exc)
        return False
    try:
        return bool(inject_memory_context_into_event(event, query_text=query_text))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[dc_router] memory injection failed: %s", exc)
        return False


async def _build_memory_query(context: Any, event: Any) -> str:
    try:
        from astrbot.core.runtime_context.memory_query import (
            build_memory_retrieval_query,
        )
    except Exception:  # noqa: BLE001
        try:
            return str(getattr(event, "message_str", "") or "")
        except Exception:  # noqa: BLE001
            return ""
    try:
        return await build_memory_retrieval_query(context, event)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[dc_router] build_memory_retrieval_query failed: %s", exc)
        return str(getattr(event, "message_str", "") or "")


async def _send_dept_memory_prompt(context: Any, event: Any, state: Any) -> None:
    """Best-effort card send with text fallback."""
    from .preprocessing.department_memory import prompt_text

    try:
        from dc_engines.feishu_card_streamer import (
            ensure_streamers_on_context,
            extract_chat_info_from_event,
            send_card_via_runtime,
        )
    except Exception:  # noqa: BLE001
        ensure_streamers_on_context = None
        extract_chat_info_from_event = None
        send_card_via_runtime = None
    sent = False
    if (
        getattr(event, "get_platform_name", lambda: "")() or ""
    ).lower() == "lark" and ensure_streamers_on_context is not None:
        try:
            streamer = ensure_streamers_on_context(context).get(
                event.get_platform_id() or ""
            )
            if streamer is not None and extract_chat_info_from_event is not None:
                chat_id, receive_id_type = extract_chat_info_from_event(event)
                if chat_id:
                    card = _build_dept_memory_card(state)
                    if send_card_via_runtime is not None:
                        stream = await send_card_via_runtime(
                            streamer,
                            card_type="daily_response",
                            chat_id=chat_id,
                            receive_id_type=receive_id_type,
                            card=card,
                            platform_id=event.get_platform_id() or "",
                            event="start",
                            detail=f"department memory prompt {state.suggestion_id}",
                        )
                        sent = stream is not None
        except Exception as exc:  # noqa: BLE001
            logger.debug("[dc_router] dept memory card send skipped: %s", exc)
    if not sent:
        try:
            from astrbot.api.event import MessageEventResult

            event.should_call_llm(False)
            event.set_result(
                MessageEventResult()
                .message(prompt_text(state))
                .use_t2i(False)
                .stop_event()
            )
        except Exception:  # noqa: BLE001
            pass


def _build_dept_memory_card(state: Any) -> dict:
    names = "、".join(state.department_names) or "相关部门"
    base = {"source": "department_memory_prompt", "suggestion_id": state.suggestion_id}
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
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "调用记忆"},
                        "type": "primary",
                        "value": {**base, "action": "confirm"},
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "不用"},
                        "type": "default",
                        "value": {**base, "action": "dismiss"},
                    },
                ],
            },
        ],
    }


def _read_dynamic_aliases() -> list[dict]:
    """Read intent aliases from assistant language overrides for v1 fallback."""
    try:
        from dc_engines.assistant_distillation import load_language_overrides

        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        data_dir = Path(get_astrbot_data_path())
        data = (
            load_language_overrides(
                data_dir / "config" / "assistant_language_overrides.json"
            )
            or {}
        )
        aliases = data.get("intent_aliases", [])
        return aliases if isinstance(aliases, list) else []
    except Exception:  # noqa: BLE001
        return []


async def _v1_fallback(
    context: Any,
    event: Any,
    text: str,
) -> bool:
    """v1.0 compatibility path used only for disabled/dry-run/error fallback."""
    from .routing.legacy_v1_fallback import V1_INTENT_TO_PROVIDER

    dynamic = _read_dynamic_aliases()

    # 1) prefix
    classification = classify_intent_v1(text, dynamic_aliases=dynamic)
    source = "prefix"
    if classification is None:
        # 2) LLM
        intent = await reason_with_llm_v1(context, text)
        if intent is not None:
            classification = SimpleNamespace(intent=intent, source="llm")
            source = "llm"
            target_provider = V1_INTENT_TO_PROVIDER.get(intent)
        else:
            # 3) keyword
            classification = classify_intent_v1(text, dynamic_aliases=dynamic)
            if classification is None:
                if not is_dc_router_managed_platform(event.get_platform_id() or ""):
                    return False
                classification = SimpleNamespace(intent="casual", source="default")
                source = "default"
            else:
                source = "keyword"
            target_provider = V1_INTENT_TO_PROVIDER.get(classification.intent)
    else:
        target_provider = V1_INTENT_TO_PROVIDER.get(classification.intent)

    if not target_provider:
        return False
    return await apply_provider_pin(
        context,
        event,
        target_provider_id=target_provider,
        source=source,
        intent=classification.intent,
        reason=f"v1.0 {source} → {target_provider}",
    )


def _cfg_dict(cfg: DCRouterConfig) -> dict:
    return {
        "enabled": cfg.enabled,
        "dry_run": cfg.dry_run,
        "fallback_on_error": cfg.fallback_on_error,
    }


async def _build_arbiter(cfg: DCRouterConfig) -> Any:
    """cfg.arbiter_enabled=true 时构造 QuotaGateArbiter，失败回退 None（透传）。"""
    if not cfg.arbiter_enabled:
        return None
    try:
        from harness import QuotaGateArbiter

        from .antigravity_health import antigravity_allowed
        from .dc_quota_runtime import get_quota_gate

        return QuotaGateArbiter(
            quota_gate=await get_quota_gate(),
            circuit_checker=antigravity_allowed,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[dc_router] QuotaGateArbiter 构造失败，回退透传仲裁: %s", exc)
        return None


async def _run_dc_router(
    context: Any,
    event: Any,
    cfg: DCRouterConfig,
    *,
    dry_run: bool,
) -> tuple[bool, str, str]:
    """调 DCRouter.decide(envelope) → apply_decision (或 annotate)。"""
    try:
        from dc_router_core.entrypoint import DCRouter
    except Exception as exc:  # noqa: BLE001
        logger.error("[dc_router] dc_router_core import failed: %s", exc)
        return False, "", ""
    try:
        router = DCRouter(arbiter=await _build_arbiter(cfg))
        envelope = build_envelope(event)
        decision = await router.decide(envelope)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[dc_router] decide() raised: %s", exc)
        return False, "", ""

    if dry_run:
        annotate_event_with_decision(event, decision)
        return (
            False,
            str(getattr(decision, "intent", "") or ""),
            str(getattr(decision, "provider_id", "") or ""),
        )

    handled = await apply_decision(context, event, decision)
    return (
        handled,
        str(getattr(decision, "intent", "") or ""),
        str(getattr(decision, "provider_id", "") or ""),
    )


async def dispatch(
    context: Any,
    event: Any,
    cfg: DCRouterConfig,
) -> DispatchResult:
    """Plugin route hook 的唯一入口。返回 handled=True 时 plugin.route() 应 return。

    cfg 必须显式传入 (plugin.py 调 load_config 后传入) — 避免 silent None。
    """
    text = str(getattr(event, "message_str", "") or "")
    platform_id = _safe_platform(event)

    # ── 1) 卡片回调 (最早) ──────────────────────────────────────────────
    if text.startswith("__card_action__:"):
        card_result = await try_handle_card_action(context, event)
        if card_result.handled:
            return DispatchResult(
                handled=True,
                source="card_action" if not card_result.stop else "card_action_stopped",
            )

    # ── 2) 斜杠命令 → 旁路 ────────────────────────────────────────────
    if _SLASH_RE.match(text):
        logger.debug("[dc_router] 斜杠命令绕过路由: %s", text[:80])
        return DispatchResult(handled=False, source="slash_command")

    # ── 3) chitchat (group event 需 at/wake) ──────────────────────────
    if is_dc_router_managed_platform(platform_id):
        chat = await try_handle_chitchat(event, text)
        if chat.handled:
            return DispatchResult(
                handled=True, source=f"chitchat:{chat.matched_intent}"
            )

    # ── 4) reasoning prefix (所有 platform 生效) ───────────────────────
    pinned = match_reasoning_prefix(text)
    if pinned:
        handled = await apply_provider_pin(
            context,
            event,
            target_provider_id=pinned,
            source="reasoning_prefix",
            intent=None,
            reason=f"user pinned prefix → {pinned}",
        )
        return DispatchResult(
            handled=handled,
            source="reasoning_prefix",
            decision_provider=pinned,
        )

    # ── 5) 飞书 channel agent (强 pin 优先) ───────────────────────────
    if is_dc_router_managed_platform(platform_id):
        handled = await try_apply_feishu_channel_route(context, event, cfg)
        if handled:
            return DispatchResult(
                handled=True,
                source="feishu_channel",
            )

    # ── 6-9) truth intake / dept memory / memory injection / tone ─────
    memory_query_text = text
    if is_dc_router_managed_platform(platform_id):
        if await _maybe_truth_intake(context, event, cfg):
            return DispatchResult(handled=True, source="truth_intake")
        memory_query_text = await _build_memory_query(context, event)
        dept_decision = try_handle_department_memory(
            event,
            raw_text=text,
            query_text=memory_query_text,
            send_prompt_response=False,
        )
        if dept_decision.stop and dept_decision.audit_state is not None:
            await _send_dept_memory_prompt(context, event, dept_decision.audit_state)
            return DispatchResult(
                handled=True,
                source="dept_memory_prompt",
                decision_intent="dept_memory_suggested",
            )
        effective_text = dept_decision.effective_text or text
        if dept_decision.inject_memory and await _memory_injection(
            event,
            query_text=dept_decision.memory_query_text or memory_query_text,
        ):
            try:
                hits = event.get_extra("dc_agent_memory_hits") or {}
                logger.info(
                    "[dc_router] memory injected platform=%s docs=%s items=%s",
                    platform_id,
                    hits.get("documents", 0),
                    hits.get("project_items", 0),
                )
            except Exception:  # noqa: BLE001
                pass
        # assistant tone — 永远走，不阻塞
        if try_inject_assistant_tone(event, effective_text):
            logger.info(
                "[dc_router] assistant tone context injected platform=%s",
                platform_id,
            )

    # ── 10) media route (后台任务，不阻塞 dispatch 后续) ──────────────
    if is_dc_router_managed_platform(platform_id):
        if await try_handle_media_route(context, event, text):
            return DispatchResult(handled=True, source="media_route")

    # ── 11) dc_router (主路径) ───────────────────────────────────────
    if cfg.is_active or cfg.is_dry_run:
        handled, intent, provider = await _run_dc_router(
            context, event, cfg, dry_run=cfg.is_dry_run
        )
        if handled:
            return DispatchResult(
                handled=True,
                source="dc_router",
                decision_intent=intent,
                decision_provider=provider,
            )
        if cfg.is_active and not cfg.fallback_on_error:
            # 异常且禁止回退
            logger.error(
                "[dc_router] decide() 异常且 fallback_on_error=false，停止本消息处理"
            )
            return DispatchResult(handled=True, source="dc_router_error_stop")
        # dry-run 或 decide 没接管 → 走 v1.0
        if cfg.is_active:
            logger.debug("[dc_router] decide() 未接管，fallback 到 v1.0")

    # ── 12) v1.0 fallback (dc_router 关闭 / 异常 / dry-run) ──────────
    handled = await _v1_fallback(context, event, text)
    return DispatchResult(
        handled=handled,
        source="v1.0" if handled else "v1.0_no_match",
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


__all__ = ["DispatchResult", "dispatch", "load_config"]
