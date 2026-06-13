"""CLI provider handlers — extracted from the legacy ``routing_adapter.py``.

This module is the *only* boundary that knows how to dispatch to local CLI
backed providers (Antigravity / Codex / Grok Build). It is invoked from
``routing.apply_decision`` when ``RouterDecision.provider_id`` starts with
``cli/`` and the router is in non-dry-run mode.

设计原则 (P3 重构 2026-06-11):
- ``routing_adapter.py`` 已成为 LEGACY；新 dispatch 路径必须独立
  handle CLI providers, 不依赖旧 ``route_via_dc_router``.
- Antigravity: circuit-breaker gated, QuotaGate-admitted, queue-card
  rendered. 失败时自动 fallback 到 ``ANTIGRAVITY_FALLBACK_PROVIDER_ID``.
- Codex: progress card + 直接 finalization. 不排队 (depth=DIRECT).
- Grok Build: 失败时自动 fallback 到 ``GROK_BUILD_FALLBACK_PROVIDER_ID``.
- 任何异常都被吞掉, 返回 False → caller 走 v1.0 fallback.

所有 ``await asyncio.create_subprocess_exec`` / ``pexpect`` 调用都被
隔离到 ``cli_runner.py``. 本模块只做编排 + 与 AstrBot 事件流对接.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import MessageEventResult
from astrbot.core.provider.entities import ProviderType

# 关键常量 - 跟 routing_adapter.py 保持一致; 后续允许从 config 注入
_DC_AGENT_ROOT = Path(__file__).resolve().parents[3]
CLI_PROVIDER_PREFIX = "cli/"
ANTIGRAVITY_PROVIDER_ID = "cli/antigravity/gemini-3.5-flash"
ANTIGRAVITY_RESOURCE_KEY = "antigravity_cli_flash"
ANTIGRAVITY_COOLDOWN_SECONDS = 5
ANTIGRAVITY_FALLBACK_PROVIDER_ID = "aihubmix/gemini-3.5-flash"
GROK_BUILD_FALLBACK_PROVIDER_ID = "aihubmix/grok-4.3"


def _safe_platform(event: Any) -> str:
    getter = getattr(event, "get_platform_id", None)
    if not callable(getter):
        return ""
    try:
        return str(getter() or "")
    except Exception:  # noqa: BLE001
        return ""


def _is_cli_provider(provider_id: str) -> bool:
    return provider_id.startswith(CLI_PROVIDER_PREFIX)


def _parse_cli_provider(provider_id: str) -> tuple[str, str, str | None]:
    """cli/<backend>/<model>[-<effort>] → (backend, model, effort).

    Pre-condition: ``provider_id`` starts with ``CLI_PROVIDER_PREFIX`` (``cli/``).
    Callers must gate with ``_is_cli_provider`` first. If a non-CLI provider
    is passed in (e.g. ``aihubmix/claude-opus-4-7`` or ``codex/gpt-5.5-medium``),
    we return ``("", provider_id, None)`` so the dispatch layer can still
    distinguish \"not a CLI provider\" from a real parsing failure — otherwise
    a non-CLI string starting with ``codex/`` would be misclassified as a
    Codex CLI backend (regression caught by
    ``tests/dc_router/test_cli_handlers.py::TestParseCliProvider::test_non_cli_provider_returns_empty_backend``
    on 2026-06-11).
    """
    if not provider_id or not provider_id.startswith(CLI_PROVIDER_PREFIX):
        return "", provider_id, None
    model = provider_id.removeprefix(CLI_PROVIDER_PREFIX)
    if model.startswith("antigravity/"):
        return "antigravity", model.removeprefix("antigravity/"), None
    if model.startswith("grok-build") or model == "grok-build":
        return "grok", model, None
    if model.startswith("codex/"):
        return "codex", model.removeprefix("codex/"), None
    if model.startswith("claude-"):
        for effort in ("xhigh", "high", "medium", "low"):
            suffix = f"-{effort}"
            if model.endswith(suffix):
                return "claude", model.removesuffix(suffix), effort
        return "claude", model, "medium"
    return "", model, None


def _replace_event_text(event: Any, text: str) -> None:
    """Best-effort: 把 event.message_str 改成我们真正想送进 LLM 的版本.
    同时尝试同步 message_obj.message_str.
    """
    try:
        event.message_str = text
    except Exception:  # noqa: BLE001
        return
    try:
        if getattr(event, "message_obj", None) is not None:
            event.message_obj.message_str = text
    except Exception:  # noqa: BLE001
        pass


def _provider_ids(context: Any) -> set[str]:
    try:
        return {p.meta().id for p in context.get_all_providers()}
    except Exception as exc:  # noqa: BLE001
        logger.debug("[cli_handlers] get_all_providers 失败: %s", exc)
        return set()


async def _switch_provider(
    context: Any,
    event: Any,
    provider_id: str,
) -> bool:
    """调 ``context.provider_manager.set_provider``, 出错返回 False."""
    umo = getattr(event, "unified_msg_origin", "") or ""
    if not umo:
        return False
    try:
        await context.provider_manager.set_provider(
            provider_id=provider_id,
            provider_type=ProviderType.CHAT_COMPLETION,
            umo=umo,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[cli_handlers] set_provider(%s) 失败: %s",
            provider_id,
            exc,
        )
        return False


def _format_wait(eta_at: float | None) -> str:
    if not eta_at:
        return "稍后"
    minutes = max(1, math.ceil((eta_at - time.time()) / 60))
    return f"约 {minutes} 分钟"


# ────────────────── Antigravity CLI ──────────────────


async def _send_antigravity_queue_message(
    event: Any,
    *,
    queue_position: int,
    eta_at: float | None,
) -> None:
    """Best-effort: 排队提示直接通过 set_result 发回, 不依赖 streamer."""
    if event is None:
        return
    msg = (
        "哎呀，现在小助手太忙啦，当前同时接待人数已经超过 9 人。"
        "我已经帮您排好队了，请您耐心等待⌛️\n\n"
        f"当前位置：第 {queue_position} 位\n"
        f"预计等待：{_format_wait(eta_at)}"
    )
    try:
        event.should_call_llm(False)
        event.set_result(MessageEventResult().message(msg).use_t2i(False).stop_event())
    except Exception:  # noqa: BLE001
        pass


async def _start_antigravity(
    context: Any,
    event: Any,
    *,
    decision: Any,
    prompt: str,
) -> bool:
    """Antigravity CLI 接管 (走 QuotaGate admit)."""
    try:
        from harness import AdmissionMode, QuotaRequest

        from .antigravity_health import (
            antigravity_allowed,
            mark_antigravity_failure,
            mark_antigravity_success,
            record_antigravity_circuit_fallback,
        )
        from .cli_runner import CliRunner
        from .dc_quota_runtime import get_quota_gate
    except Exception as exc:  # noqa: BLE001
        logger.error("[cli_handlers] antigravity 依赖 import 失败: %s", exc)
        return False

    allowed, reason, state = antigravity_allowed()
    if not allowed:
        record_antigravity_circuit_fallback(reason=reason, state=state)
        available = _provider_ids(context)
        if ANTIGRAVITY_FALLBACK_PROVIDER_ID in available:
            if await _switch_provider(context, event, ANTIGRAVITY_FALLBACK_PROVIDER_ID):
                event.set_extra(
                    "dc_router_antigravity_health_fallback",
                    {
                        "provider_id": decision.provider_id,
                        "fallback_provider_id": ANTIGRAVITY_FALLBACK_PROVIDER_ID,
                        "reason": reason,
                        "remaining_seconds": state.get("remaining_seconds", 0),
                    },
                )
                logger.info(
                    "[cli_handlers] Antigravity circuit open · fallback=%s",
                    ANTIGRAVITY_FALLBACK_PROVIDER_ID,
                )
                return True
        return False

    backend, model, _effort = _parse_cli_provider(decision.provider_id)
    if backend != "antigravity":
        logger.warning(
            "[cli_handlers] decision.provider_id=%s 不是 antigravity backend, 不接管",
            decision.provider_id,
        )
        return False

    original_prompt = (event.message_str or "").strip()
    platform_id = _safe_platform(event)
    gate = await get_quota_gate()
    try:
        admission = await gate.admit(
            QuotaRequest(
                primary_resource_key=ANTIGRAVITY_RESOURCE_KEY,
                resource_keys=(ANTIGRAVITY_RESOURCE_KEY,),
                payload={
                    "provider_id": decision.provider_id,
                    "backend": backend,
                    "intent": decision.intent,
                    "model": model,
                    "umo": event.unified_msg_origin,
                    "prompt": prompt,
                    "original_prompt": original_prompt,
                    "platform_id": platform_id,
                },
                requested_by=event.get_sender_id() or None,
                session_id=event.unified_msg_origin,
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[cli_handlers] QuotaGate.admit 失败: %s", exc)
        return False

    mode_value = (
        admission.mode.value
        if hasattr(admission.mode, "value")
        else str(admission.mode)
    )
    if mode_value == AdmissionMode.QUEUED.value:
        await _send_antigravity_queue_message(
            event,
            queue_position=admission.queue_position or 1,
            eta_at=admission.eta_at,
        )
        event.set_extra(
            "dc_router_antigravity_queue",
            {
                "provider_id": decision.provider_id,
                "fallback_provider_id": ANTIGRAVITY_FALLBACK_PROVIDER_ID,
                "job_id": admission.job.job_id,
                "queue_position": admission.queue_position or 1,
                "eta_at": admission.eta_at,
            },
        )
        return True

    try:
        result = await CliRunner(cwd=_DC_AGENT_ROOT).run_antigravity(
            prompt, model=model, timeout=90
        )
    except Exception as exc:  # noqa: BLE001
        mark_antigravity_failure(error_code="exception", error=str(exc))
        try:
            await gate.fail(
                admission.job.job_id,
                f"Antigravity CLI exception: {exc}",
                retry_after_seconds=ANTIGRAVITY_COOLDOWN_SECONDS,
            )
        except Exception:  # noqa: BLE001
            pass
        return False

    if not result.ok:
        mark_antigravity_failure(error_code=result.error_code, error=result.error)
        try:
            await gate.fail(
                admission.job.job_id,
                f"{result.error_code or 'cli_error'}: {result.error or ''}",
                retry_after_seconds=ANTIGRAVITY_COOLDOWN_SECONDS,
            )
        except Exception:  # noqa: BLE001
            pass
        # 失败 fallback
        available = _provider_ids(context)
        if ANTIGRAVITY_FALLBACK_PROVIDER_ID in available:
            if await _switch_provider(context, event, ANTIGRAVITY_FALLBACK_PROVIDER_ID):
                event.set_extra(
                    "dc_router_antigravity_failure_fallback",
                    {
                        "provider_id": decision.provider_id,
                        "fallback_provider_id": ANTIGRAVITY_FALLBACK_PROVIDER_ID,
                        "error_code": result.error_code,
                    },
                )
                return True
        return False

    mark_antigravity_success(elapsed_sec=result.elapsed_sec)
    try:
        await gate.complete(
            admission.job.job_id,
            result={
                "provider_id": decision.provider_id,
                "elapsed_sec": result.elapsed_sec,
            },
            cooldown_seconds=ANTIGRAVITY_COOLDOWN_SECONDS,
        )
    except Exception:  # noqa: BLE001
        pass

    event.set_extra("dc_router_cli_provider", decision.provider_id)
    event.should_call_llm(False)
    try:
        event.set_result(
            MessageEventResult().message(result.text).use_t2i(False).stop_event()
        )
    except Exception:  # noqa: BLE001
        pass
    logger.info(
        "[cli_handlers] Antigravity direct success provider=%s model=%s elapsed=%.2fs",
        decision.provider_id,
        model,
        result.elapsed_sec,
    )
    return True


# ────────────────── Codex CLI ──────────────────


async def _start_codex(
    _context: Any,
    event: Any,
    *,
    decision: Any,
    prompt: str,
) -> bool:
    """Codex CLI 接管 (DIRECT, 不排队)."""
    try:
        from .cli_runner import CliRunner
    except Exception as exc:  # noqa: BLE001
        logger.error("[cli_handlers] cli_runner import 失败: %s", exc)
        return False

    backend, model, _effort = _parse_cli_provider(decision.provider_id)
    if backend != "codex":
        return False

    try:
        result = await CliRunner(cwd=_DC_AGENT_ROOT).run_codex(
            prompt, model=model, timeout=300
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[cli_handlers] Codex CLI exception: %s", exc)
        return False

    if not result.ok:
        logger.warning(
            "[cli_handlers] Codex CLI failed code=%s err=%s",
            result.error_code,
            result.error,
        )
        return False

    event.set_extra("dc_router_cli_provider", decision.provider_id)
    event.should_call_llm(False)
    try:
        event.set_result(
            MessageEventResult().message(result.text).use_t2i(False).stop_event()
        )
    except Exception:  # noqa: BLE001
        pass
    logger.info(
        "[cli_handlers] Codex direct success provider=%s model=%s elapsed=%.2fs",
        decision.provider_id,
        model,
        result.elapsed_sec,
    )
    return True


# ────────────────── Grok Build CLI ──────────────────


async def _start_grok(
    context: Any,
    event: Any,
    *,
    decision: Any,
    prompt: str,
) -> bool:
    """Grok Build CLI 接管 (DIRECT, 失败 → aihubmix/grok-4.3)."""
    try:
        from grok_worker import get_grok_worker
    except Exception as exc:  # noqa: BLE001
        logger.warning("[cli_handlers] grok_worker import 失败: %s", exc)
        return False

    try:
        result = await get_grok_worker().ask_public_opinion(prompt)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[cli_handlers] Grok Build 调用异常: %s, fallback=%s",
            exc,
            GROK_BUILD_FALLBACK_PROVIDER_ID,
        )
        result = None

    if result is None or not result.ok:
        error_code = (
            result.error_code if result is not None else "exception"
        ) or "unknown"
        available = _provider_ids(context)
        if GROK_BUILD_FALLBACK_PROVIDER_ID in available:
            if await _switch_provider(context, event, GROK_BUILD_FALLBACK_PROVIDER_ID):
                _replace_event_text(event, prompt)
                event.set_extra(
                    "dc_router_grok_build_fallback",
                    {
                        "provider_id": decision.provider_id,
                        "fallback_provider_id": GROK_BUILD_FALLBACK_PROVIDER_ID,
                        "error_code": error_code,
                    },
                )
                return True
        return False

    event.set_extra("dc_router_cli_provider", decision.provider_id)
    event.set_extra("dc_router_grok_build_elapsed_sec", result.elapsed_sec)
    event.should_call_llm(False)
    try:
        event.set_result(
            MessageEventResult().message(result.text).use_t2i(False).stop_event()
        )
    except Exception:  # noqa: BLE001
        pass
    logger.info(
        "[cli_handlers] Grok direct success provider=%s elapsed=%.2fs",
        decision.provider_id,
        result.elapsed_sec,
    )
    return True


# ────────────────── 公开入口 ──────────────────


def build_cli_prompt(event: Any, decision: Any) -> str:
    """依据 intent 拼接最简 prompt (避免 routing_adapter 那种重 prompt 模板)."""
    text = (event.message_str or "").strip()
    intent = str(getattr(decision, "intent", "") or "")
    if intent in {"casual", "work_preflight", "realtime", "fallback"}:
        return (
            f"你是巅池-Agent小助手。\n"
            "请用自然、简短、亲切的中文直接回答员工问题，不要解释路由过程。\n\n"
            f"员工消息：{text}"
        )
    if intent in {"deep_insight", "deep_creative"}:
        return (
            f"你是巅池-Agent小助手。\n"
            "请用中文给员工深度回答。结论先行，结构清晰，避免空话。\n\n"
            f"员工任务：{text}"
        )
    if intent == "public_opinion":
        return (
            f"你是巅池-Agent小助手，负责舆情管理和危机公关建议。\n"
            "请用中文直接回答。\n\n"
            f"员工问题：{text}"
        )
    return text


async def dispatch_cli_provider(
    context: Any,
    event: Any,
    decision: Any,
) -> bool:
    """``apply_decision`` 调这里. 返回 True 表示已接管, False 表示让
    v1.0 fallback 处理. 内部只处理 provider_id 形如 ``cli/...`` 的情况.
    """
    provider_id = str(getattr(decision, "provider_id", "") or "")
    if not _is_cli_provider(provider_id):
        return False

    backend, _, _ = _parse_cli_provider(provider_id)
    prompt = build_cli_prompt(event, decision)

    if backend == "antigravity":
        return await _start_antigravity(
            context, event, decision=decision, prompt=prompt
        )
    if backend == "codex":
        return await _start_codex(context, event, decision=decision, prompt=prompt)
    if backend == "grok":
        return await _start_grok(context, event, decision=decision, prompt=prompt)
    logger.warning(
        "[cli_handlers] 未支持的 CLI backend=%s (provider_id=%s)",
        backend,
        provider_id,
    )
    return False


# 处理 Antigravity 排队卡动作 (来自 card_action handler)
async def handle_antigravity_queue_card_action(
    context: Any,
    event: Any,
) -> bool:
    """用户点 Antigravity 排队卡「用池池小助手先处理」→ 取消队列 + 切 fallback.

    返回 True 表示事件已被处理 (set_result 已调).
    """
    text = event.message_str or ""
    if not text.startswith("__card_action__:"):
        return False
    try:
        payload = json.loads(text[len("__card_action__:") :])
    except Exception:  # noqa: BLE001
        return False
    value = payload.get("value", {}) or {}
    if value.get("source") != "antigravity_queue_card":
        return False
    if value.get("action") != "use_fallback":
        event.should_call_llm(False)
        event.set_result(MessageEventResult().message("").use_t2i(False).stop_event())
        return True

    job_id = str(value.get("job_id") or "").strip()
    available = _provider_ids(context)
    if ANTIGRAVITY_FALLBACK_PROVIDER_ID not in available:
        event.should_call_llm(False)
        event.set_result(
            MessageEventResult()
            .message("池池小助手现在也有点忙，我先继续帮您保留排队位置。")
            .use_t2i(False)
            .stop_event()
        )
        return True

    try:
        from .dc_quota_runtime import get_quota_gate

        gate = await get_quota_gate()
        cancelled = await gate.cancel_pending_job(job_id, reason="user fallback")
    except Exception as exc:  # noqa: BLE001
        logger.warning("[cli_handlers] cancel antigravity queue 失败: %s", exc)
        cancelled = False

    if not cancelled:
        event.should_call_llm(False)
        event.set_result(
            MessageEventResult()
            .message("池池小助手现在也有点忙，我先继续帮您保留排队位置。")
            .use_t2i(False)
            .stop_event()
        )
        return True

    if await _switch_provider(context, event, ANTIGRAVITY_FALLBACK_PROVIDER_ID):
        event.set_extra("dc_router_antigravity_fallback_job_id", job_id)
        return True
    return False


# ────────────────── Background queue recovery ──────────────────
# P3 (2026-06-11): 从 routing_adapter.py 接管 background queue recovery
# 任务 — 每 60s 扫描 QuotaGate 数据库, 重新启动已冷却的 pending CLI 任务.

_QUEUE_RECOVERY_TASK: asyncio.Task | None = None
_DEFAULT_RECOVERY_INTERVAL = 60


async def _resume_pending_cli_jobs(_context: Any, gate: Any, *, limit: int = 20) -> int:
    """Re-admit pending QuotaGate CLI jobs (antigravity / codex) when resources free.

    Returns the number of jobs that were successfully restarted.
    """
    resumed = 0
    try:
        pending_jobs = await gate.list_pending_jobs(limit=limit)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[cli_handlers] 读取 pending jobs 失败: %s", exc)
        return 0

    for pending in pending_jobs:
        payload = pending.payload
        provider_id = str(payload.get("provider_id") or "")
        if not provider_id.startswith(CLI_PROVIDER_PREFIX):
            continue
        backend = str(payload.get("backend") or "")
        if backend not in {"antigravity", "codex"}:
            continue
        job = await gate.start_pending_job(pending.job_id)
        if job is None:
            continue
        resumed += 1
        logger.info(
            "[cli_handlers] 恢复 pending CLI job=%s provider=%s",
            job.job_id,
            provider_id,
        )
    return resumed


async def _queue_recovery_loop(
    context: Any,
    *,
    interval_seconds: int = _DEFAULT_RECOVERY_INTERVAL,
) -> None:
    """Background task: 每 N 秒扫描 QuotaGate, 恢复 pending CLI 任务."""
    try:
        from .dc_quota_runtime import get_quota_gate
    except Exception as exc:  # noqa: BLE001
        logger.error("[cli_handlers] 启动 queue_recovery 失败: %s", exc)
        return

    gate = await get_quota_gate()
    while True:
        try:
            resumed = await _resume_pending_cli_jobs(context, gate)
            if resumed:
                logger.info("[cli_handlers] 恢复了 %s 个 pending CLI 任务", resumed)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("[cli_handlers] queue_recovery 扫描失败: %s", exc)
        await asyncio.sleep(interval_seconds)


def start_queue_recovery(
    context: Any,
    *,
    interval_seconds: int = _DEFAULT_RECOVERY_INTERVAL,
) -> None:
    """Start one background scanner for persisted pending queued CLI jobs.

    Idempotent: 如果已经在跑, 不再启动第二个 task.
    """
    global _QUEUE_RECOVERY_TASK
    if _QUEUE_RECOVERY_TASK is not None and not _QUEUE_RECOVERY_TASK.done():
        return
    _QUEUE_RECOVERY_TASK = asyncio.create_task(
        _queue_recovery_loop(context, interval_seconds=interval_seconds)
    )
    logger.info(
        "[cli_handlers] queue_recovery 启动 · interval=%ss",
        interval_seconds,
    )


def stop_queue_recovery() -> None:
    """Stop the background pending queue scanner."""
    global _QUEUE_RECOVERY_TASK
    if _QUEUE_RECOVERY_TASK is not None and not _QUEUE_RECOVERY_TASK.done():
        _QUEUE_RECOVERY_TASK.cancel()
    _QUEUE_RECOVERY_TASK = None


__all__ = [
    "ANTIGRAVITY_FALLBACK_PROVIDER_ID",
    "ANTIGRAVITY_PROVIDER_ID",
    "ANTIGRAVITY_RESOURCE_KEY",
    "CLI_PROVIDER_PREFIX",
    "GROK_BUILD_FALLBACK_PROVIDER_ID",
    "build_cli_prompt",
    "dispatch_cli_provider",
    "handle_antigravity_queue_card_action",
    "is_cli_provider",
    "parse_cli_provider",
    "start_queue_recovery",
    "stop_queue_recovery",
]


# 给旧 ``is_cli_provider`` / ``parse_cli_provider`` 别名兼容
is_cli_provider = _is_cli_provider
parse_cli_provider = _parse_cli_provider
