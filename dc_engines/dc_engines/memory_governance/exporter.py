"""Export raw NAS memory candidates into Obsidian governance notes."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import GovernedMemory
from .obsidian_codec import apply_note_path, render_governance_note
from .store import MemoryGovernanceStore

GOVERNANCE_ROOT = Path("40_MemoryGovernance")
INBOX_DIR = GOVERNANCE_ROOT / "Inbox"


@dataclass(slots=True)
class ExportResult:
    exported_count: int = 0
    skipped_count: int = 0
    memory_ids: list[str] = field(default_factory=list)
    note_paths: list[Path] = field(default_factory=list)


def export_memory_candidates(
    *,
    nas_db_path: Path | str,
    vault_path: Path | str,
    store: MemoryGovernanceStore,
    limit: int = 50,
    now: str,
) -> ExportResult:
    """Export need-review NAS documents to Obsidian governance notes."""

    store.initialize()
    nas_db_path = Path(nas_db_path)
    vault_path = Path(vault_path)
    inbox_dir = vault_path / INBOX_DIR
    inbox_dir.mkdir(parents=True, exist_ok=True)

    result = ExportResult()
    for row in _fetch_candidate_rows(nas_db_path, limit):
        memory = _memory_from_document_row(row, now=now)
        existing = store.get_memory(memory.memory_id)
        if existing and existing.review_status != "need_review":
            result.skipped_count += 1
            continue

        note_path = inbox_dir / f"{memory.memory_id}.md"
        memory = apply_note_path(memory, str(note_path))
        note_path.write_text(render_governance_note(memory), encoding="utf-8")
        store.upsert_memory(memory)
        result.exported_count += 1
        result.memory_ids.append(memory.memory_id)
        result.note_paths.append(note_path)

    return result


def stable_nas_memory_id(source_path: str, source_hash: str) -> str:
    """Build a deterministic governed memory id for a NAS source document."""

    digest = hashlib.sha1(f"{source_path}:{source_hash}".encode()).hexdigest()
    return f"mem_nas_{digest[:12]}"


def _fetch_candidate_rows(nas_db_path: Path, limit: int) -> list[sqlite3.Row]:
    if not nas_db_path.exists() or limit < 1:
        return []
    with sqlite3.connect(nas_db_path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            """
            SELECT doc_key, rel_path, source_path, sha256, parser, title, summary,
                   tags_json, indexed_at, metadata_json, project_id, project_name,
                   doc_type, owner, confidence, review_status
            FROM documents
            WHERE COALESCE(review_status, 'need_review') = 'need_review'
              AND COALESCE(source_path, '') != ''
            ORDER BY indexed_at DESC, doc_key
            LIMIT ?
            """,
            (limit,),
        ).fetchall()


def _memory_from_document_row(row: sqlite3.Row, *, now: str) -> GovernedMemory:
    source_path = str(row["source_path"] or "")
    raw_hash = str(row["sha256"] or "")
    source_hash = raw_hash if raw_hash.startswith("sha256:") else f"sha256:{raw_hash}"
    summary = str(row["summary"] or "")
    title = str(row["title"] or row["rel_path"] or row["doc_key"])
    tags = _json_list(row["tags_json"])
    if "dc-agent-memory" not in tags:
        tags.insert(0, "dc-agent-memory")
    doc_type = str(row["doc_type"] or "")
    if doc_type:
        tags.append(f"doc_type:{doc_type}")
    project_name = str(row["project_name"] or "")
    if project_name:
        tags.append(f"project:{project_name}")

    confidence = float(row["confidence"] or 0)
    if confidence <= 0:
        confidence = 0.5
    confidence = min(max(confidence, 0), 1)

    return GovernedMemory(
        memory_id=stable_nas_memory_id(source_path, source_hash),
        source_system="nas",
        source_id=f"nas:{row['doc_key']}",
        source_path=source_path,
        source_hash=source_hash,
        title=title,
        summary=summary,
        canonical_text=summary or title,
        memory_kind=_memory_kind_from_doc_type(doc_type),
        review_status="need_review",
        confidence=confidence,
        sensitivity="internal",
        owner=str(row["owner"] or ""),
        project_id=str(row["project_id"] or ""),
        tags=_dedupe(tags),
        links=[f"[[{Path(source_path).stem or title}]]"],
        obsidian_note_path="",
        governance_version=1,
        created_at=now,
        updated_at=now,
        approved_at="",
        approved_by="",
    )


def _memory_kind_from_doc_type(doc_type: str) -> str:
    if doc_type in {"SOP", "执行方案", "排期分工"}:
        return "process"
    if doc_type in {"项目总表", "复盘结算", "预算报价"}:
        return "project"
    return "document"


def _json_list(value: Any) -> list[str]:
    try:
        parsed = json.loads(str(value or "[]"))
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if str(item).strip()]


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
