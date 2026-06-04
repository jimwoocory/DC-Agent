"""Promote approved governed memories into runtime-facing stores."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import GovernedMemory
from .store import MemoryGovernanceStore

PROMOTABLE_REVIEW_STATUSES = {"approved"}
PROMOTABLE_SENSITIVITIES = {"public", "internal"}


@dataclass(slots=True)
class PromotionResult:
    promoted_memory_ids: list[str] = field(default_factory=list)
    skipped_memory_ids: list[str] = field(default_factory=list)
    dry_run: bool = False


def promote_governed_memories(
    *,
    store: MemoryGovernanceStore,
    nas_db_path: Path | str,
    overrides_path: Path | str,
    now: str,
    actor: str = "memory-governance-promoter",
    dry_run: bool = False,
    limit: int = 10000,
) -> PromotionResult:
    """Promote approved governed memories to NAS metadata and override config."""

    store.initialize()
    nas_db_path = Path(nas_db_path)
    overrides_path = Path(overrides_path)
    result = PromotionResult(dry_run=dry_run)
    memories = store.list_memories(limit=limit)
    promotable = [memory for memory in memories if _is_promotable(memory)]
    skipped = [memory for memory in memories if not _is_promotable(memory)]
    result.promoted_memory_ids = [memory.memory_id for memory in promotable]
    result.skipped_memory_ids = [memory.memory_id for memory in skipped]

    if dry_run:
        return result

    overrides = _load_overrides(overrides_path)
    for memory in promotable:
        _promote_to_nas_db(memory, nas_db_path)
        _promote_to_overrides(memory, overrides)
        store.append_audit(
            memory.memory_id,
            "promoted_to_recall",
            actor,
            {
                "source_id": memory.source_id,
                "source_path": memory.source_path,
                "review_status": memory.review_status,
                "sensitivity": memory.sensitivity,
            },
            created_at=now,
        )
    if promotable:
        _save_overrides(overrides_path, overrides)
    return result


def _is_promotable(memory: GovernedMemory) -> bool:
    return (
        memory.review_status in PROMOTABLE_REVIEW_STATUSES
        and memory.sensitivity in PROMOTABLE_SENSITIVITIES
    )


def _promote_to_nas_db(memory: GovernedMemory, nas_db_path: Path) -> None:
    if memory.source_system != "nas" or not nas_db_path.exists():
        return
    doc_key = _nas_doc_key(memory.source_id)
    if not doc_key:
        return
    with sqlite3.connect(nas_db_path) as conn:
        conn.execute(
            """
            UPDATE documents
            SET review_status = 'confirmed',
                owner = COALESCE(NULLIF(?, ''), owner),
                project_id = COALESCE(NULLIF(?, ''), project_id),
                confidence = MAX(COALESCE(confidence, 0), ?)
            WHERE doc_key = ?
            """,
            (memory.owner, memory.project_id, memory.confidence, doc_key),
        )


def _promote_to_overrides(memory: GovernedMemory, overrides: dict[str, Any]) -> None:
    projects = overrides.setdefault("projects", {})
    evidence = [
        f"governed_memory:{memory.memory_id}",
        f"source:{memory.source_path or memory.source_id}",
    ]
    projects[memory.title] = {
        "project_name": memory.title,
        "owner": memory.owner,
        "project_id": memory.project_id,
        "confidence": memory.confidence,
        "review_status": "confirmed",
        "evidence": evidence,
    }


def _nas_doc_key(source_id: str) -> str:
    if not source_id.startswith("nas:"):
        return ""
    return source_id.removeprefix("nas:")


def _load_overrides(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"people": {}, "projects": {}, "departments": {}, "ownership_rules": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"people": {}, "projects": {}, "departments": {}, "ownership_rules": []}
    data.setdefault("people", {})
    data.setdefault("projects", {})
    data.setdefault("departments", {})
    data.setdefault("ownership_rules", [])
    return data


def _save_overrides(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    tmp.replace(path)
