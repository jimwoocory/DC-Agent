from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

HarnessTaskStatus = Literal[
    "pending",
    "in_progress",
    "blocked",
    "review_required",
    "completed",
    "cancelled",
    "failed",
]

HarnessReviewDecision = Literal[
    "approved",
    "rejected",
]

HARNESS_TERMINAL_STATUSES: set[HarnessTaskStatus] = {
    "completed",
    "cancelled",
    "failed",
}


@dataclass(slots=True)
class HarnessTaskCreateRequest:
    title: str
    conversation_id: str
    platform_id: str
    session_id: str
    domain: str = "general"
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class HarnessTask:
    task_id: str
    conversation_id: str
    platform_id: str
    session_id: str
    title: str
    domain: str
    status: HarnessTaskStatus
    payload: dict[str, Any]
    result: dict[str, Any]
    created_at: str
    updated_at: str


@dataclass(slots=True)
class HarnessTaskEvent:
    event_id: str
    task_id: str
    event_type: str
    payload: dict[str, Any]
    created_at: str


@dataclass(slots=True)
class HarnessTaskReview:
    review_id: str
    task_id: str
    reviewer_id: str
    decision: HarnessReviewDecision
    note: str
    created_at: str
