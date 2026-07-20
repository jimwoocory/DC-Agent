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
    unchanged_memory_ids: list[str] = field(default_factory=list)
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
    """Reconcile approved governed memories into runtime-facing stores.

    Args:
        store: Governed memory persistence store.
        nas_db_path: NAS memory database to reconcile.
        overrides_path: Runtime override JSON path to reconcile.
        now: ISO timestamp for state-changing promotion audits.
        actor: Actor recorded in promotion audits.
        dry_run: Whether to report changes without writing them.
        limit: Maximum number of governed memories to scan.

    Returns:
        Promotion, unchanged, and ineligible memory identifiers.
    """

    store.initialize()
    nas_db_path = Path(nas_db_path)
    overrides_path = Path(overrides_path)
    result = PromotionResult(dry_run=dry_run)
    memories = store.list_memories(limit=limit)
    promotable = [memory for memory in memories if _is_promotable(memory)]
    skipped = [memory for memory in memories if not _is_promotable(memory)]
    result.skipped_memory_ids = [memory.memory_id for memory in skipped]

    overrides = _load_overrides(overrides_path)
    overrides_changed = False
    for memory in promotable:
        nas_changed = _promote_to_nas_db(memory, nas_db_path, dry_run=dry_run)
        override_changed = _promote_to_overrides(
            memory,
            overrides,
            dry_run=dry_run,
        )
        if not nas_changed and not override_changed:
            result.unchanged_memory_ids.append(memory.memory_id)
            continue
        result.promoted_memory_ids.append(memory.memory_id)
        overrides_changed = overrides_changed or override_changed
        if dry_run:
            continue
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
    if overrides_changed and not dry_run:
        _save_overrides(overrides_path, overrides)
    return result


def _is_promotable(memory: GovernedMemory) -> bool:
    return (
        memory.review_status in PROMOTABLE_REVIEW_STATUSES
        and memory.sensitivity in PROMOTABLE_SENSITIVITIES
    )


def _promote_to_nas_db(
    memory: GovernedMemory,
    nas_db_path: Path,
    *,
    dry_run: bool,
) -> bool:
    """Reconcile one governed memory into its NAS document row.

    Args:
        memory: Approved governed memory to reconcile.
        nas_db_path: NAS memory database path.
        dry_run: Whether to detect changes without applying them.

    Returns:
        Whether the NAS projection differs from the desired state.
    """

    if memory.source_system != "nas" or not nas_db_path.exists():
        return False
    doc_key = _nas_doc_key(memory.source_id)
    if not doc_key:
        return False
    with sqlite3.connect(nas_db_path) as conn:
        row = conn.execute(
            """
            SELECT review_status, owner, project_id, confidence
            FROM documents
            WHERE doc_key = ?
            """,
            (doc_key,),
        ).fetchone()
        if row is None:
            return False
        desired_owner = memory.owner or str(row[1] or "")
        desired_project_id = memory.project_id or str(row[2] or "")
        desired_confidence = max(float(row[3] or 0), memory.confidence)
        changed = (
            str(row[0] or "") != "confirmed"
            or str(row[1] or "") != desired_owner
            or str(row[2] or "") != desired_project_id
            or float(row[3] or 0) != desired_confidence
        )
        if not changed or dry_run:
            return changed
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
    return True


def _promote_to_overrides(
    memory: GovernedMemory,
    overrides: dict[str, Any],
    *,
    dry_run: bool,
) -> bool:
    """Reconcile one governed memory into runtime overrides.

    Args:
        memory: Approved governed memory to reconcile.
        overrides: Parsed runtime override document.
        dry_run: Whether to detect changes without applying them.

    Returns:
        Whether the override projection differs from the desired state.
    """

    projects = overrides.setdefault("projects", {})
    evidence = [
        f"governed_memory:{memory.memory_id}",
        f"source:{memory.source_path or memory.source_id}",
    ]
    desired = {
        "project_name": memory.title,
        "owner": memory.owner,
        "project_id": memory.project_id,
        "confidence": memory.confidence,
        "review_status": "confirmed",
        "evidence": evidence,
    }
    changed = projects.get(memory.title) != desired
    if changed and not dry_run:
        projects[memory.title] = desired
    return changed


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
