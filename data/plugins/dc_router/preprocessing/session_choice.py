"""Closed-loop Feishu conversation routing after completed assistant tasks."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any
from weakref import WeakValueDictionary

from dc_engines.assistant_workbench_cards import (
    build_assistant_session_choice_card,
)
from dc_engines.card_style import apply_card_visual_system

from astrbot.api import logger

SESSION_CHOICE_ACTIONS = frozenset({"open_new_session", "continue_current_session"})
_DECISION_LOCKS: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


@dataclass(slots=True)
class SessionChoiceActionResult:
    """Outcome returned to the shared workbench card-action handler.

    Attributes:
        handled: Whether the callback belongs to the session-choice card.
        state: Durable decision state after processing.
        message: Optional retryable error shown to the user.
    """

    handled: bool
    state: str = ""
    message: str = ""


def _task_store(context: Any) -> Any | None:
    store = getattr(context, "harness_store", None)
    if store is not None:
        return store
    engine = getattr(context, "harness_engine", None)
    return getattr(engine, "store", None)


def _is_private_lark_event(event: Any) -> bool:
    try:
        platform_name = str(event.get_platform_name() or "").lower()
        group_id = str(event.get_group_id() or "")
    except Exception:  # noqa: BLE001
        return False
    return platform_name == "lark" and not group_id


def _sender_id(event: Any) -> str:
    try:
        return str(event.get_sender_id() or "")
    except Exception:  # noqa: BLE001
        return ""


async def _patch_session_choice_card(
    context: Any,
    *,
    platform_id: str,
    message_id: str,
    state: str,
) -> bool:
    """Patch one decision card independently of in-memory stream state.

    Args:
        context: AstrBot runtime context containing the Feishu platform.
        platform_id: AstrBot platform instance identifier.
        message_id: Original Feishu card message identifier.
        state: Durable decision state to project.

    Returns:
        True when Feishu accepted the terminal card patch.
    """
    if not message_id:
        return False
    try:
        from lark_oapi.api.im.v1 import (
            PatchMessageRequest,
            PatchMessageRequestBody,
        )

        platform = None
        get_platform_inst = getattr(context, "get_platform_inst", None)
        if callable(get_platform_inst):
            platform = get_platform_inst(platform_id)
        if platform is None:
            manager = getattr(context, "platform_manager", None)
            for item in getattr(manager, "platform_insts", None) or []:
                meta = getattr(item, "meta", None)
                item_id = ""
                if callable(meta):
                    item_id = str(getattr(meta(), "id", "") or "")
                if item_id == platform_id:
                    platform = item
                    break
        client = getattr(platform, "lark_api", None)
        if client is None:
            return False
        card = apply_card_visual_system(
            build_assistant_session_choice_card(state=state)
        )
        body = (
            PatchMessageRequestBody.builder()
            .content(json.dumps(card, ensure_ascii=False))
            .build()
        )
        request = (
            PatchMessageRequest.builder()
            .message_id(message_id)
            .request_body(body)
            .build()
        )
        response = await client.im.v1.message.apatch(request)
        return bool(response.success())
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[dc_router] session choice card patch failed message=%s: %s",
            message_id,
            exc,
        )
        return False


async def _sync_terminal_card(context: Any, decision: Any) -> bool:
    """Project a terminal decision into its Feishu card and audit the result.

    Args:
        context: AstrBot runtime context.
        decision: Durable Harness session-decision record.

    Returns:
        True when the card is synchronized or no card was ever delivered.
    """
    store = _task_store(context)
    if store is None:
        return False
    message_id = str(getattr(decision, "card_message_id", "") or "")
    if not message_id:
        return True
    state = str(getattr(decision, "state", "") or "")
    ok = await _patch_session_choice_card(
        context,
        platform_id=str(getattr(decision, "platform_id", "") or ""),
        message_id=message_id,
        state=state,
    )
    await store.mark_session_decision_card_patch(
        str(getattr(decision, "decision_id", "") or ""),
        "synced" if ok else "failed",
    )
    return ok


async def send_session_choice(
    context: Any,
    *,
    task_id: str,
    unified_msg_origin: str,
    platform_id: str,
    chat_id: str,
    receive_id_type: str,
    event: Any | None = None,
) -> bool:
    """Persist and send one post-result session-choice card.

    Args:
        context: AstrBot runtime context.
        task_id: Completed task or deterministic workbench result identifier.
        unified_msg_origin: AstrBot session owning the current conversation.
        platform_id: AstrBot platform instance identifier.
        chat_id: Feishu card delivery destination.
        receive_id_type: Feishu destination identifier type.
        event: Original event when the shared workbench sender can be reused.

    Returns:
        True when a new or retried pending card is delivered.
    """
    store = _task_store(context)
    manager = getattr(context, "conversation_manager", None)
    if store is None or manager is None or not task_id or not unified_msg_origin:
        return False
    source_conversation_id = await manager.get_curr_conversation_id(unified_msg_origin)
    if not source_conversation_id:
        return False
    decision, previous, created = await store.create_session_decision(
        task_id=task_id,
        unified_msg_origin=unified_msg_origin,
        platform_id=platform_id,
        chat_id=chat_id,
        source_conversation_id=source_conversation_id,
    )
    if previous is not None:
        await _sync_terminal_card(context, previous)
    if not created and str(getattr(decision, "card_message_id", "") or ""):
        return False

    card = build_assistant_session_choice_card(decision.decision_id)
    stream = None
    if event is not None:
        from .assistant_workbench import _send_card

        stream = await _send_card(
            context,
            event,
            card_type="assistant_session_choice",
            card=card,
        )
    else:
        try:
            from dc_engines.card_runtime import send_card_via_runtime
            from dc_engines.feishu_card_streamer import ensure_streamers_on_context

            streamer = ensure_streamers_on_context(context).get(platform_id)
            if streamer is not None:
                stream = await send_card_via_runtime(
                    streamer,
                    card_type="assistant_session_choice",
                    chat_id=chat_id,
                    receive_id_type=receive_id_type,
                    card=card,
                    platform_id=platform_id,
                    event="start",
                    detail="completed assistant task session choice",
                    task_id=task_id,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[dc_router] session choice send failed: %s", exc)
    message_id = str(getattr(stream, "message_id", "") or "")
    if not message_id:
        await store.mark_session_decision_card_patch(decision.decision_id, "failed")
        return False
    await store.bind_session_decision_card(decision.decision_id, message_id)
    return True


async def send_session_choice_for_event(context: Any, event: Any) -> bool:
    """Send a decision card after a successful synchronous workbench result.

    Args:
        context: AstrBot runtime context.
        event: Completed AstrBot message event.

    Returns:
        True when the post-result decision card was sent.
    """
    if not _is_private_lark_event(event):
        return False
    get_extra = getattr(event, "get_extra", None)
    if not callable(get_extra):
        return False
    task_type = str(get_extra("assistant_workbench_task_type") or "")
    if not task_type or get_extra("dc_media_route_handled"):
        return False
    if get_extra("_llm_error_message"):
        return False
    result = getattr(event, "get_result", lambda: None)()
    if result is None or (
        not getattr(result, "chain", None)
        and get_extra("dc_result_delivery_succeeded") is not True
    ):
        return False
    quality = str(get_extra("dc_result_response_quality") or "")
    if not quality:
        try:
            from dc_engines.harness.runtime_hooks import classify_response_quality

            quality = classify_response_quality(
                None,
                str(result.get_plain_text(with_other_comps_mark=True) or ""),
            )
        except Exception:  # noqa: BLE001
            quality = ""
    if quality and quality != "success":
        return False

    store = _task_store(context)
    task_ids = [
        str(get_extra(key) or "").strip()
        for key in (
            "dc_truth_intake_task_id",
            "department_workflow_task_id",
            "workflow_intent_task_id",
        )
    ]
    task_ids = [task_id for task_id in task_ids if task_id]
    if store is not None and task_ids:
        tasks = [await store.get_task(task_id) for task_id in task_ids]
        existing_tasks = [task for task in tasks if task is not None]
        if existing_tasks and any(
            task.status != "completed" for task in existing_tasks
        ):
            return False

    platform_id = str(event.get_platform_id() or "")
    from dc_engines.feishu_card_streamer import extract_chat_info_from_event

    chat_id, receive_id_type = extract_chat_info_from_event(event)
    logical_task_id = str(
        get_extra("assistant_workbench_decision_task_id")
        or (task_ids[0] if task_ids else "")
    ).strip()
    return await send_session_choice(
        context,
        task_id=logical_task_id,
        unified_msg_origin=str(getattr(event, "unified_msg_origin", "") or ""),
        platform_id=platform_id,
        chat_id=chat_id,
        receive_id_type=receive_id_type,
        event=event,
    )


async def resolve_pending_session_choice_for_message(
    context: Any,
    event: Any,
) -> bool:
    """Treat a normal next message as an implicit continue decision.

    Args:
        context: AstrBot runtime context.
        event: Incoming user message before normal routing.

    Returns:
        True when a pending decision was resolved.
    """
    if not _is_private_lark_event(event):
        return False
    text = str(getattr(event, "message_str", "") or "")
    if text.startswith("__card_action__:"):
        return False
    store = _task_store(context)
    umo = str(getattr(event, "unified_msg_origin", "") or "")
    if store is None or not umo:
        return False
    decision = await store.get_pending_session_decision(umo)
    if decision is None:
        return False
    resolved = await store.continue_session_decision(
        decision.decision_id,
        operator_id=_sender_id(event),
        decision_source="implicit_message",
    )
    if resolved is None or resolved.state != "continue_current":
        return False
    await _sync_terminal_card(context, resolved)
    return True


async def handle_session_choice_action(
    context: Any,
    event: Any,
    *,
    value: dict[str, Any],
    payload: dict[str, Any],
) -> SessionChoiceActionResult:
    """Execute one trusted new-or-continue card callback idempotently.

    Args:
        context: AstrBot runtime context.
        event: Trusted Feishu card callback event.
        value: Callback value containing only action and decision id.
        payload: Normalized callback envelope with original message metadata.

    Returns:
        Handled state and an optional retryable error message.
    """
    action = str(value.get("action") or "")
    if action not in SESSION_CHOICE_ACTIONS:
        return SessionChoiceActionResult(handled=False)
    decision_id = str(value.get("decision_id") or "").strip()
    store = _task_store(context)
    if store is None or not decision_id:
        return SessionChoiceActionResult(
            handled=True,
            message="会话选择记录不可用，请直接发送下一条消息继续当前对话。",
        )
    decision = await store.get_session_decision(decision_id)
    if decision is None:
        return SessionChoiceActionResult(
            handled=True,
            message="这张会话选择卡已失效，请直接发送下一条消息。",
        )
    umo = str(getattr(event, "unified_msg_origin", "") or "")
    platform_id = str(event.get_platform_id() or "")
    message_id = str(payload.get("open_message_id") or "")
    if (
        umo != decision.unified_msg_origin
        or platform_id != decision.platform_id
        or (message_id and decision.card_message_id != message_id)
    ):
        return SessionChoiceActionResult(
            handled=True,
            message="这张卡不属于当前会话，未执行切换。",
        )

    lock = _DECISION_LOCKS.setdefault(decision_id, asyncio.Lock())
    async with lock:
        try:
            if action == "continue_current_session":
                decision = await store.continue_session_decision(
                    decision_id,
                    operator_id=_sender_id(event),
                    decision_source="card_click",
                )
            else:
                decision = await store.begin_new_session_decision(
                    decision_id,
                    operator_id=_sender_id(event),
                )
            if decision is None:
                return SessionChoiceActionResult(
                    handled=True,
                    message="会话选择记录不存在，请直接发送下一条消息。",
                )
            if decision.state == "opening_new":
                manager = getattr(context, "conversation_manager", None)
                if manager is None:
                    raise RuntimeError("conversation manager unavailable")
                current_id = await manager.get_curr_conversation_id(
                    decision.unified_msg_origin
                )
                if current_id and current_id != decision.source_conversation_id:
                    target_id = current_id
                else:
                    persona_id = None
                    if decision.source_conversation_id:
                        source = await manager.get_conversation(
                            decision.unified_msg_origin,
                            decision.source_conversation_id,
                        )
                        persona_id = (
                            getattr(source, "persona_id", None) if source else None
                        )
                    target_id = await manager.new_conversation(
                        decision.unified_msg_origin,
                        decision.platform_id,
                        persona_id=persona_id,
                    )
                decision = await store.complete_new_session_decision(
                    decision_id,
                    target_conversation_id=target_id,
                )
            if decision is not None and decision.state in {
                "new_conversation",
                "continue_current",
                "superseded_continue",
            }:
                await _sync_terminal_card(context, decision)
                return SessionChoiceActionResult(
                    handled=True,
                    state=decision.state,
                )
            return SessionChoiceActionResult(
                handled=True,
                state=str(getattr(decision, "state", "") or ""),
                message="会话切换尚未完成，请稍后重试。",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[dc_router] session choice action failed decision=%s: %s",
                decision_id,
                exc,
            )
            return SessionChoiceActionResult(
                handled=True,
                state=str(getattr(decision, "state", "") or ""),
                message="会话切换暂时失败，请稍后再次点击。",
            )


__all__ = [
    "SESSION_CHOICE_ACTIONS",
    "SessionChoiceActionResult",
    "handle_session_choice_action",
    "resolve_pending_session_choice_for_message",
    "send_session_choice",
    "send_session_choice_for_event",
]
