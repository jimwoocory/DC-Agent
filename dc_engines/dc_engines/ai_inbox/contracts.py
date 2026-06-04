from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

InboxItemCategory = Literal[
    "request",
    "task",
    "feedback",
    "material",
    "escalation",
    "question",
    "other",
]

InboxItemStatus = Literal[
    "new",
    "acknowledged",
    "waiting_materials",
    "in_progress",
    "delivered",
    "confirmed",
    "closed",
    "ignored",
]

INBOX_OPEN_STATUSES: set[InboxItemStatus] = {
    "new",
    "acknowledged",
    "waiting_materials",
    "in_progress",
    "delivered",
}


@dataclass(slots=True)
class InboxItemCreateRequest:
    session_id: str
    conversation_id: str
    platform_id: str
    sender_id: str
    sender_name: str
    text: str
    category: InboxItemCategory
    status: InboxItemStatus = "new"
    case_id: str = ""
    task_id: str = ""
    source: str = "ai_inbox_plugin"
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class InboxItem:
    item_id: str
    session_id: str
    conversation_id: str
    platform_id: str
    sender_id: str
    sender_name: str
    text: str
    category: InboxItemCategory
    status: InboxItemStatus
    case_id: str
    task_id: str
    source: str
    payload: dict[str, Any]
    created_at: str
    updated_at: str


@dataclass(slots=True)
class InboxEvent:
    event_id: str
    item_id: str
    event_type: str
    payload: dict[str, Any]
    created_at: str
