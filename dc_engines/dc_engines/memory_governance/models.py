"""Data models for the governed memory system."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

MemorySourceSystem = Literal["nas", "astrbot_kb", "conversation", "manual", "harness"]
MemoryKind = Literal[
    "document",
    "fact",
    "project",
    "person",
    "process",
    "preference",
    "decision",
]
ReviewStatus = Literal[
    "need_review",
    "approved",
    "rejected",
    "merged",
    "sensitive_blocked",
    "stale",
]
Sensitivity = Literal["public", "internal", "confidential", "secret"]

VALID_SOURCE_SYSTEMS = {
    "nas",
    "astrbot_kb",
    "conversation",
    "manual",
    "harness",
}
VALID_MEMORY_KINDS = {
    "document",
    "fact",
    "project",
    "person",
    "process",
    "preference",
    "decision",
}
VALID_REVIEW_STATUSES = {
    "need_review",
    "approved",
    "rejected",
    "merged",
    "sensitive_blocked",
    "stale",
}
VALID_SENSITIVITIES = {"public", "internal", "confidential", "secret"}


@dataclass(slots=True)
class GovernedMemory:
    """Canonical memory row after it enters the governance pipeline."""

    memory_id: str
    source_system: MemorySourceSystem
    source_id: str
    source_path: str
    source_hash: str
    title: str
    summary: str
    canonical_text: str
    memory_kind: MemoryKind
    review_status: ReviewStatus
    confidence: float
    sensitivity: Sensitivity
    owner: str = ""
    project_id: str = ""
    tags: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    obsidian_note_path: str = ""
    governance_version: int = 1
    created_at: str = ""
    updated_at: str = ""
    approved_at: str = ""
    approved_by: str = ""

    def __post_init__(self) -> None:
        _require_non_empty("memory_id", self.memory_id)
        _require_non_empty("source_system", self.source_system)
        _require_non_empty("source_id", self.source_id)
        _require_non_empty("source_hash", self.source_hash)
        _require_non_empty("title", self.title)
        _require_non_empty("canonical_text", self.canonical_text)
        _require_member("source_system", self.source_system, VALID_SOURCE_SYSTEMS)
        _require_member("memory_kind", self.memory_kind, VALID_MEMORY_KINDS)
        _require_member("review_status", self.review_status, VALID_REVIEW_STATUSES)
        _require_member("sensitivity", self.sensitivity, VALID_SENSITIVITIES)
        if not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
        if self.governance_version < 1:
            raise ValueError("governance_version must be >= 1")


@dataclass(slots=True)
class ReviewDecision:
    """Append-only record of a human or system review decision."""

    decision_id: str
    memory_id: str
    decision: ReviewStatus
    reviewer: str
    reason: str
    before: dict[str, Any]
    after: dict[str, Any]
    created_at: str

    def __post_init__(self) -> None:
        _require_non_empty("decision_id", self.decision_id)
        _require_non_empty("memory_id", self.memory_id)
        _require_non_empty("decision", self.decision)
        _require_non_empty("reviewer", self.reviewer)
        _require_member("decision", self.decision, VALID_REVIEW_STATUSES)


def _require_non_empty(field_name: str, value: str) -> None:
    if not str(value or "").strip():
        raise ValueError(f"{field_name} is required")


def _require_member(field_name: str, value: str, allowed: set[str]) -> None:
    if value not in allowed:
        raise ValueError(f"{field_name} must be one of {sorted(allowed)}")
