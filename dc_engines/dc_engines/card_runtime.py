"""Runtime gateway for Feishu card sends.

This module is the stable path for application code. Scripts may call it, but
runtime plugins should also call it directly so card sends are registered,
observable and fallback-safe instead of being ad-hoc Feishu API calls.
"""

from __future__ import annotations

from typing import Any

from .card_system import (
    CARD_REGISTRY,
    archive_card_result,
    card_runtime_message_context,
    mark_card_result_retracted,
    record_card_action_event,
    record_card_runtime_event,
)
from .feishu_card_streamer import CardStream, FeishuCardStreamer


def assert_registered_card(card_type: str) -> None:
    if card_type not in CARD_REGISTRY:
        raise KeyError(f"unknown card_type: {card_type}")


def record_card_action_via_runtime(
    *,
    message_id: str,
    conversation_id: str = "",
    action: str = "",
    source: str = "",
    task_id: str = "",
    operator_id: str = "",
    is_regeneration: bool | None = None,
    platform_id: str = "",
) -> None:
    """Record a minimal Feishu button callback through the shared runtime.

    Args:
        message_id: Original Feishu card message identifier.
        conversation_id: Opaque Feishu chat identifier.
        action: Button action name only.
        source: Card callback source label.
        task_id: Associated task identifier.
        operator_id: Feishu operator identifier, retained only as a digest.
        is_regeneration: Explicit regeneration marker, or ``None`` to infer it.
        platform_id: AstrBot platform instance identifier.

    Returns:
        None.
    """
    record_card_action_event(
        message_id=message_id,
        conversation_id=conversation_id,
        action=action,
        source=source,
        task_id=task_id,
        operator_id=operator_id,
        is_regeneration=is_regeneration,
        platform_id=platform_id,
    )


async def patch_card_via_runtime(
    streamer: FeishuCardStreamer,
    *,
    card_type: str,
    message_id: str,
    card: dict[str, Any],
    platform_id: str = "",
    chat_id: str = "",
    receive_id_type: str = "",
    event: str = "patch",
    detail: str = "",
    fallback: str = "plain_text",
) -> bool:
    """Patch one registered card and record the runtime outcome.

    Args:
        streamer: Shared Feishu card streamer.
        card_type: Registered card type projected into the existing message.
        message_id: Existing Feishu card message identifier.
        card: Full replacement Card JSON payload.
        platform_id: AstrBot platform instance identifier.
        chat_id: Feishu conversation identifier used for runtime telemetry.
        receive_id_type: Feishu recipient identifier type.
        event: Runtime lifecycle event name.
        detail: Short operational detail.
        fallback: Fallback behavior when patching fails.

    Returns:
        True when Feishu accepts the patch, otherwise False.
    """
    assert_registered_card(card_type)
    try:
        ok = await streamer.patch(message_id, card)
        runtime_detail = detail or (
            "card patched" if ok else "streamer.patch returned false"
        )
    except Exception as exc:  # noqa: BLE001
        ok = False
        runtime_detail = f"{type(exc).__name__}: {exc}"
    record_card_runtime_event(
        event=event,
        card_type=card_type,
        ok=ok,
        platform_id=platform_id,
        chat_id=chat_id,
        receive_id_type=receive_id_type,
        message_id=message_id,
        detail=runtime_detail,
        fallback="" if ok else fallback,
    )
    return ok


async def send_card_via_runtime(
    streamer: FeishuCardStreamer,
    *,
    card_type: str,
    chat_id: str,
    receive_id_type: str,
    card: dict[str, Any],
    platform_id: str = "",
    event: str = "start",
    detail: str = "",
    fallback: str = "plain_text",
    source: str = "",
    task_id: str = "",
    source_message_id: str = "",
    delivery_files: list[str | dict[str, Any]] | tuple[str | dict[str, Any], ...] = (),
    regeneration_of_message_id: str = "",
    archive_result: bool | None = None,
) -> CardStream | None:
    """Send one registered card and record the outcome.

    Returning None means the caller must keep its fallback path alive. This is
    intentional: card rendering should improve UX, never make the user lose a
    response.

    Args:
        streamer: Shared Feishu card streamer.
        card_type: Registered card type.
        chat_id: Feishu chat or recipient identifier.
        receive_id_type: Feishu recipient identifier type.
        card: Card JSON to send.
        platform_id: AstrBot platform instance identifier.
        event: Runtime lifecycle event name.
        detail: Short operational detail.
        fallback: Fallback behavior when sending fails.
        source: Business source associated with the formal result.
        task_id: Associated task or generation record identifier.
        source_message_id: User/source message that initiated the result.
        delivery_files: Delivered file, URL, or artifact references.
        regeneration_of_message_id: Parent result for a regenerated output.
        archive_result: Explicitly override the registered archive policy.

    Returns:
        The active card stream, or ``None`` when sending failed.
    """
    assert_registered_card(card_type)
    try:
        stream = await streamer.start(
            chat_id=chat_id,
            receive_id_type=receive_id_type,
            card=card,
        )
    except Exception as exc:  # noqa: BLE001
        record_card_runtime_event(
            event=event,
            card_type=card_type,
            ok=False,
            platform_id=platform_id,
            chat_id=chat_id,
            receive_id_type=receive_id_type,
            detail=f"{type(exc).__name__}: {exc}",
            fallback=fallback,
        )
        return None

    if stream is not None:
        stream.card_type = card_type
        stream.platform_id = platform_id
        stream.runtime_event_recorder = record_card_runtime_event

    record_card_runtime_event(
        event=event,
        card_type=card_type,
        ok=stream is not None,
        platform_id=platform_id,
        chat_id=chat_id,
        receive_id_type=receive_id_type,
        message_id=stream.message_id if stream else "",
        detail=detail or ("card sent" if stream else "streamer.start returned None"),
        fallback="" if stream else fallback,
    )
    should_archive = (
        archive_result
        if archive_result is not None
        else "send" in CARD_REGISTRY[card_type].archive_events
        and event not in {"grey_push", "manual_real_push"}
    )
    if stream is not None and should_archive:
        try:
            archived = archive_card_result(
                archive_event="send",
                card_type=card_type,
                message_id=stream.message_id,
                conversation_id=chat_id,
                card=stream.last_card or card,
                platform_id=platform_id,
                source=source,
                task_id=task_id,
                source_message_id=source_message_id,
                delivery_files=delivery_files,
                regeneration_of_message_id=regeneration_of_message_id,
            )
            archive_ok = archived is not None
            archive_detail = (
                "formal result archived"
                if archive_ok
                else "formal result archive skipped"
            )
        except Exception as exc:  # noqa: BLE001
            archive_ok = False
            archive_detail = f"{type(exc).__name__}: {exc}"
        record_card_runtime_event(
            event="result_archive",
            card_type=card_type,
            ok=archive_ok,
            platform_id=platform_id,
            chat_id=chat_id,
            receive_id_type=receive_id_type,
            message_id=stream.message_id,
            detail=archive_detail,
        )

        def archive_updated_result(updated_card: dict[str, Any]) -> None:
            try:
                archived_update = archive_card_result(
                    archive_event="update",
                    card_type=card_type,
                    message_id=stream.message_id,
                    conversation_id=chat_id,
                    card=updated_card,
                    platform_id=platform_id,
                    source=source,
                    task_id=task_id,
                    source_message_id=source_message_id,
                    delivery_files=delivery_files,
                    regeneration_of_message_id=regeneration_of_message_id,
                )
                update_ok = archived_update is not None
                update_detail = (
                    "formal result update archived"
                    if update_ok
                    else "formal result update archive skipped"
                )
            except Exception as exc:  # noqa: BLE001
                update_ok = False
                update_detail = f"{type(exc).__name__}: {exc}"
            record_card_runtime_event(
                event="result_archive_update",
                card_type=card_type,
                ok=update_ok,
                platform_id=platform_id,
                chat_id=chat_id,
                receive_id_type=receive_id_type,
                message_id=stream.message_id,
                detail=update_detail,
            )

        def archive_retracted_result(retracted_message_id: str) -> None:
            try:
                retract_archived = mark_card_result_retracted(retracted_message_id)
                retract_detail = (
                    "formal result retraction archived"
                    if retract_archived
                    else "formal result retraction archive missing"
                )
            except Exception as exc:  # noqa: BLE001
                retract_archived = False
                retract_detail = f"{type(exc).__name__}: {exc}"
            record_card_runtime_event(
                event="result_archive_retract",
                card_type=card_type,
                ok=retract_archived,
                platform_id=platform_id,
                chat_id=chat_id,
                receive_id_type=receive_id_type,
                message_id=retracted_message_id,
                detail=retract_detail,
            )

        stream.runtime_result_archiver = archive_updated_result
        stream.runtime_retract_recorder = archive_retracted_result
    return stream


async def finalize_card_via_runtime(
    streamer: FeishuCardStreamer,
    *,
    card_type: str,
    message_id: str,
    card: dict[str, Any],
    platform_id: str = "",
    detail: str = "",
    fallback: str = "plain_text",
    retract_after_sec: float | None = None,
    source: str = "",
    task_id: str = "",
    source_message_id: str = "",
    delivery_files: list[str | dict[str, Any]] | tuple[str | dict[str, Any], ...] = (),
    regeneration_of_message_id: str = "",
    archive_result: bool | None = None,
) -> bool:
    """Finalize one registered card and retain formal result evidence.

    Args:
        streamer: Shared Feishu card streamer.
        card_type: Registered terminal card type.
        message_id: Feishu message identifier to patch.
        card: Final Card JSON.
        platform_id: AstrBot platform instance identifier.
        detail: Short operational detail.
        fallback: Fallback behavior when finalization fails.
        retract_after_sec: Optional grace period before message deletion.
        source: Business source associated with the formal result.
        task_id: Associated task or generation record identifier.
        source_message_id: User/source message that initiated the result.
        delivery_files: Delivered file, URL, or artifact references.
        regeneration_of_message_id: Parent result for a regenerated output.
        archive_result: Explicitly override the registered archive policy.

    Returns:
        True when the terminal Feishu patch succeeded.
    """
    assert_registered_card(card_type)
    stream = None
    get_stream = getattr(streamer, "get_stream", None)
    if callable(get_stream):
        stream = get_stream(message_id)
    conversation_id = str(getattr(stream, "chat_id", "") or "")
    receive_id_type = str(getattr(stream, "receive_id_type", "") or "")
    if not conversation_id:
        persisted_context = card_runtime_message_context(message_id)
        conversation_id = persisted_context.get("conversation_id", "")
        receive_id_type = persisted_context.get("receive_id_type", "")
        platform_id = platform_id or persisted_context.get("platform_id", "")
    try:
        ok = await streamer.finalize(message_id, card)
    except Exception as exc:  # noqa: BLE001
        record_card_runtime_event(
            event="finalize",
            card_type=card_type,
            ok=False,
            platform_id=platform_id,
            chat_id=conversation_id,
            receive_id_type=receive_id_type,
            message_id=message_id,
            detail=f"{type(exc).__name__}: {exc}",
            fallback=fallback,
        )
        return False

    record_card_runtime_event(
        event="finalize",
        card_type=card_type,
        ok=ok,
        platform_id=platform_id,
        chat_id=conversation_id,
        receive_id_type=receive_id_type,
        message_id=message_id,
        detail=detail or ("card finalized" if ok else "streamer.finalize failed"),
        fallback="" if ok else fallback,
    )
    should_archive = (
        archive_result
        if archive_result is not None
        else "finalize" in CARD_REGISTRY[card_type].archive_events
    )
    if ok and should_archive:
        try:
            archived = archive_card_result(
                archive_event="finalize",
                card_type=card_type,
                message_id=message_id,
                conversation_id=conversation_id,
                card=card,
                platform_id=platform_id,
                source=source,
                task_id=task_id,
                source_message_id=source_message_id,
                delivery_files=delivery_files,
                regeneration_of_message_id=regeneration_of_message_id,
            )
            archive_ok = archived is not None
            archive_detail = (
                "formal result archived"
                if archive_ok
                else "formal result archive skipped"
            )
        except Exception as exc:  # noqa: BLE001
            archive_ok = False
            archive_detail = f"{type(exc).__name__}: {exc}"
        record_card_runtime_event(
            event="result_archive",
            card_type=card_type,
            ok=archive_ok,
            platform_id=platform_id,
            chat_id=conversation_id,
            receive_id_type=receive_id_type,
            message_id=message_id,
            detail=archive_detail,
        )
    if ok and retract_after_sec is not None:

        def on_retract_result(retracted: bool) -> None:
            record_card_runtime_event(
                event="retract",
                card_type=card_type,
                ok=retracted,
                platform_id=platform_id,
                chat_id=conversation_id,
                receive_id_type=receive_id_type,
                message_id=message_id,
                detail="card retracted" if retracted else "streamer.retract failed",
                fallback="" if retracted else fallback,
            )
            if retracted and should_archive:
                try:
                    retract_archived = mark_card_result_retracted(message_id)
                    retract_archive_detail = (
                        "formal result retraction archived"
                        if retract_archived
                        else "formal result retraction archive missing"
                    )
                except Exception as exc:  # noqa: BLE001
                    retract_archived = False
                    retract_archive_detail = f"{type(exc).__name__}: {exc}"
                record_card_runtime_event(
                    event="result_archive_retract",
                    card_type=card_type,
                    ok=retract_archived,
                    platform_id=platform_id,
                    chat_id=conversation_id,
                    receive_id_type=receive_id_type,
                    message_id=message_id,
                    detail=retract_archive_detail,
                )

        try:
            try:
                task = streamer.schedule_retract(
                    message_id,
                    retract_after_sec,
                    on_result=on_retract_result,
                )
            except TypeError:
                task = streamer.schedule_retract(message_id, retract_after_sec)
            scheduled = task is not None
            retract_detail = (
                f"card retract scheduled after {max(0.0, retract_after_sec):.1f}s"
                if scheduled
                else "streamer.schedule_retract returned None"
            )
        except Exception as exc:  # noqa: BLE001
            scheduled = False
            retract_detail = f"{type(exc).__name__}: {exc}"
        record_card_runtime_event(
            event="retract_scheduled",
            card_type=card_type,
            ok=scheduled,
            platform_id=platform_id,
            message_id=message_id,
            detail=retract_detail,
            fallback="" if scheduled else fallback,
        )
    return ok
