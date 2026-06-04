"""Dataclasses and typed dicts shared across the group summary modules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TypedDict


class GroupMessage(TypedDict):
    """One normalized chat message used as LLM summarization input.

    The fetcher's job is to produce a uniform stream from heterogeneous
    platforms (QQ / Lark / WebChat). Downstream code never touches the
    platform-native message shape.
    """

    sender: str
    """Display name (e.g. ``"张三"``); falls back to ``sender_id`` if unknown."""

    sender_id: str
    """Platform-native sender identifier, used for de-duplicating bot messages."""

    content: str
    """Plain-text content. Non-text segments (images, files, replies) are
    flattened to a short marker like ``"[图片]"`` so the LLM still sees them."""

    timestamp: datetime
    """UTC-normalized send time. Used for ordering and the markdown header."""

    is_bot: bool
    """True if the message was sent by DC-Agent itself, so callers can skip it."""


@dataclass(slots=True)
class GroupSummary:
    """Four-section summary returned to the group.

    ``raw_response`` keeps the LLM's exact JSON for debugging / observability;
    the four section fields are post-parse strings ready to render.
    """

    client_feedback: str
    boss_requests: str
    execution_issues: str
    stage_conclusions: str
    message_count: int
    time_range: str
    raw_response: str
