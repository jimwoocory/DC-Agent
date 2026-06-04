"""Import reviewed Obsidian governance notes into the governed memory store."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .models import GovernedMemory, ReviewDecision
from .obsidian_codec import apply_note_path, parse_governance_note
from .store import MemoryGovernanceStore

GOVERNANCE_ROOT = Path("40_MemoryGovernance")


@dataclass(slots=True)
class ImportResult:
    imported_count: int = 0
    decision_count: int = 0
    audit_count: int = 0
    memory_ids: list[str] = field(default_factory=list)
    note_paths: list[Path] = field(default_factory=list)


def import_governance_notes(
    *,
    vault_path: Path | str,
    store: MemoryGovernanceStore,
    now: str,
    actor: str = "obsidian-governance-import",
) -> ImportResult:
    """Import all governance notes from an Obsidian vault."""

    store.initialize()
    vault_path = Path(vault_path)
    root = vault_path / GOVERNANCE_ROOT
    result = ImportResult()
    if not root.exists():
        return result

    for note_path in sorted(root.rglob("*.md")):
        markdown = note_path.read_text(encoding="utf-8")
        memory = apply_note_path(
            parse_governance_note(markdown, now=now),
            str(note_path),
        )
        frontmatter = _read_frontmatter(markdown)
        existing = store.get_memory(memory.memory_id)
        before = _decision_state(existing)
        after = _decision_state(memory)

        store.upsert_memory(memory)
        store.append_audit(
            memory.memory_id,
            "import_governance_note",
            actor,
            {
                "note_path": str(note_path),
                "review_status": memory.review_status,
                "source_path": memory.source_path,
            },
            created_at=now,
        )

        result.imported_count += 1
        result.audit_count += 1
        result.memory_ids.append(memory.memory_id)
        result.note_paths.append(note_path)

        if _should_record_decision(existing, memory):
            reviewer = str(frontmatter.get("reviewer") or actor).strip() or actor
            reason = str(frontmatter.get("review_reason") or "").strip()
            decision = ReviewDecision(
                decision_id=f"dec_{uuid.uuid4().hex}",
                memory_id=memory.memory_id,
                decision=memory.review_status,
                reviewer=reviewer,
                reason=reason or "Imported Obsidian governance note.",
                before=before,
                after=after,
                created_at=now,
            )
            store.record_decision(decision)
            result.decision_count += 1

    return result


def _should_record_decision(
    existing: GovernedMemory | None,
    imported: GovernedMemory,
) -> bool:
    if imported.review_status == "need_review":
        return False
    if existing is None:
        return True
    return _decision_state(existing) != _decision_state(imported)


def _decision_state(memory: GovernedMemory | None) -> dict[str, Any]:
    if memory is None:
        return {}
    return {
        "review_status": memory.review_status,
        "canonical_text": memory.canonical_text,
        "sensitivity": memory.sensitivity,
        "owner": memory.owner,
        "project_id": memory.project_id,
        "source_hash": memory.source_hash,
    }


def _read_frontmatter(markdown: str) -> dict[str, Any]:
    if not markdown.startswith("---\n"):
        return {}
    try:
        _, yaml_text, _ = markdown.split("---", 2)
    except ValueError:
        return {}
    parsed = yaml.safe_load(yaml_text) or {}
    return parsed if isinstance(parsed, dict) else {}
