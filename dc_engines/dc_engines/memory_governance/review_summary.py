"""Daily review summary helpers for Obsidian memory governance."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .models import GovernedMemory
from .store import MemoryGovernanceStore


@dataclass(slots=True)
class ReviewSummary:
    """Pure payload for a daily memory review card."""

    total_need_review: int
    by_owner: dict[str, int] = field(default_factory=dict)
    by_source_system: dict[str, int] = field(default_factory=dict)
    items: list[dict[str, Any]] = field(default_factory=list)

    def to_card_payload(self) -> dict[str, Any]:
        return {
            "title": "Obsidian Memory Review",
            "metrics": {
                "need_review": self.total_need_review,
                "owners": self.by_owner,
                "source_systems": self.by_source_system,
            },
            "items": self.items,
        }

    def to_markdown(self) -> str:
        lines = [
            "# Obsidian Memory Review",
            "",
            f"待审 {self.total_need_review}",
            "",
            "## Sources",
        ]
        lines.extend(_count_lines(self.by_source_system))
        lines.extend(["", "## Owners"])
        lines.extend(_count_lines(self.by_owner))
        lines.extend(["", "## Queue"])
        if not self.items:
            lines.append("- No memories waiting for review.")
        for item in self.items:
            owner = item["owner"] or "unassigned"
            lines.append(
                f"- {item['title']} ({item['memory_id']}, {item['source_system']}, {owner})"
            )
        return "\n".join(lines)


def build_review_summary(
    *,
    store: MemoryGovernanceStore,
    limit: int = 10,
) -> ReviewSummary:
    """Build a no-side-effect summary of memories waiting for human review."""

    store.initialize()
    all_need_review = store.list_memories(status="need_review", limit=100000)
    visible_items = all_need_review[: max(limit, 0)]
    return ReviewSummary(
        total_need_review=len(all_need_review),
        by_owner=_count_by(all_need_review, "owner", fallback="unassigned"),
        by_source_system=_count_by(all_need_review, "source_system"),
        items=[_memory_item(memory) for memory in visible_items],
    )


def _memory_item(memory: GovernedMemory) -> dict[str, Any]:
    return {
        "memory_id": memory.memory_id,
        "title": memory.title,
        "source_system": memory.source_system,
        "owner": memory.owner,
        "updated_at": memory.updated_at,
        "obsidian_note_path": memory.obsidian_note_path,
    }


def _count_by(
    memories: list[GovernedMemory],
    field_name: str,
    *,
    fallback: str = "",
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for memory in memories:
        value = str(getattr(memory, field_name) or fallback).strip()
        if not value:
            value = fallback
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _count_lines(counts: dict[str, int]) -> list[str]:
    if not counts:
        return ["- none"]
    return [f"- {key}: {value}" for key, value in counts.items()]
