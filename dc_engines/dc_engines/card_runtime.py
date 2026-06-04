"""Runtime gateway for Feishu card sends.

This module is the stable path for application code. Scripts may call it, but
runtime plugins should also call it directly so card sends are registered,
observable and fallback-safe instead of being ad-hoc Feishu API calls.
"""

from __future__ import annotations

from typing import Any

from .card_system import CARD_REGISTRY, record_card_runtime_event
from .feishu_card_streamer import CardStream, FeishuCardStreamer


def assert_registered_card(card_type: str) -> None:
    if card_type not in CARD_REGISTRY:
        raise KeyError(f"unknown card_type: {card_type}")


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
) -> CardStream | None:
    """Send one registered card and record the outcome.

    Returning None means the caller must keep its fallback path alive. This is
    intentional: card rendering should improve UX, never make the user lose a
    response.
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
) -> bool:
    """Finalize one registered card and record the outcome."""
    assert_registered_card(card_type)
    try:
        ok = await streamer.finalize(message_id, card)
    except Exception as exc:  # noqa: BLE001
        record_card_runtime_event(
            event="finalize",
            card_type=card_type,
            ok=False,
            platform_id=platform_id,
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
        message_id=message_id,
        detail=detail or ("card finalized" if ok else "streamer.finalize failed"),
        fallback="" if ok else fallback,
    )
    return ok
