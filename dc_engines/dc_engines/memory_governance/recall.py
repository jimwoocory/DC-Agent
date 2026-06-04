"""Recall helpers for governed memory."""

from __future__ import annotations

from .models import GovernedMemory
from .store import MemoryGovernanceStore

DEFAULT_RECALL_STATUSES = {"approved"}
DEFAULT_RECALL_SENSITIVITIES = {"public", "internal"}


def list_recall_memories(
    *,
    store: MemoryGovernanceStore,
    query: str = "",
    include_unreviewed: bool = False,
    include_sensitive: bool = False,
    limit: int = 5,
) -> list[GovernedMemory]:
    """List memories eligible for runtime recall."""

    if limit < 1:
        return []
    store.initialize()
    candidates = store.list_memories(limit=10000)
    query_text = query.strip().lower()
    filtered = [
        memory
        for memory in candidates
        if _status_allowed(memory, include_unreviewed)
        and _sensitivity_allowed(memory, include_sensitive)
        and _matches_query(memory, query_text)
    ]
    filtered.sort(key=_recall_sort_key)
    return filtered[:limit]


def _status_allowed(memory: GovernedMemory, include_unreviewed: bool) -> bool:
    if include_unreviewed:
        return True
    return memory.review_status in DEFAULT_RECALL_STATUSES


def _sensitivity_allowed(memory: GovernedMemory, include_sensitive: bool) -> bool:
    if include_sensitive:
        return True
    return memory.sensitivity in DEFAULT_RECALL_SENSITIVITIES


def _matches_query(memory: GovernedMemory, query: str) -> bool:
    if not query:
        return True
    haystack = " ".join(
        [
            memory.title,
            memory.summary,
            memory.canonical_text,
            memory.owner,
            memory.project_id,
            " ".join(memory.tags),
        ]
    ).lower()
    return query in haystack


def _recall_sort_key(memory: GovernedMemory) -> tuple[int, int, str]:
    sensitivity_rank = 0 if memory.sensitivity in DEFAULT_RECALL_SENSITIVITIES else 1
    status_rank = 0 if memory.review_status == "approved" else 1
    return (sensitivity_rank, status_rank, memory.memory_id)
