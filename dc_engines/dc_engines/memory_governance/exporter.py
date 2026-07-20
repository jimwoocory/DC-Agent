"""Export raw NAS memory candidates into Obsidian governance notes."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dc_engines.harness.memory_store import HarnessMemoryRecord

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


@dataclass(slots=True, frozen=True)
class TaskOutcomeCandidateExport:
    """Result of exporting one task outcome to governed memory.

    Attributes:
        memory_id: Stable governed-memory candidate identifier.
        status: Candidate publication result.
        note_path: Obsidian review note path.
    """

    memory_id: str
    status: str
    note_path: Path


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
        if existing:
            if existing.review_status != "need_review":
                result.skipped_count += 1
                continue
            existing_note_path = str(existing.obsidian_note_path or "").strip()
            if existing_note_path and Path(existing_note_path).exists():
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


def export_content_sop_memory_candidate(
    *,
    candidate: dict[str, Any],
    vault_path: Path | str,
    store: MemoryGovernanceStore,
    now: str,
) -> ExportResult:
    """Export a Content SOP spiral memory candidate to Obsidian governance Inbox."""

    store.initialize()
    vault_path = Path(vault_path)
    inbox_dir = vault_path / INBOX_DIR
    inbox_dir.mkdir(parents=True, exist_ok=True)
    result = ExportResult()

    if str(candidate.get("review_status") or "") != "need_review":
        result.skipped_count += 1
        return result

    memory = _memory_from_content_sop_candidate(candidate, now=now)
    existing = store.get_memory(memory.memory_id)
    if existing:
        if existing.review_status != "need_review":
            result.skipped_count += 1
            return result
        existing_note_path = str(existing.obsidian_note_path or "").strip()
        if existing_note_path and Path(existing_note_path).exists():
            result.skipped_count += 1
            return result

    note_path = inbox_dir / f"{memory.memory_id}.md"
    memory = apply_note_path(memory, str(note_path))
    note_path.write_text(render_governance_note(memory), encoding="utf-8")
    store.upsert_memory(memory)
    result.exported_count = 1
    result.memory_ids.append(memory.memory_id)
    result.note_paths.append(note_path)
    return result


def export_task_outcome_candidates(
    *,
    harness_memory_db_path: Path | str,
    vault_path: Path | str,
    store: MemoryGovernanceStore,
    limit: int = 20,
    now: str,
) -> ExportResult:
    """Export harness task_outcome memories to Obsidian governance Inbox.

    打通任务记忆与治理记忆：harness_memory.db 里由 HarnessMemoryPromoter
    写入的 task_outcome 记忆，导出为 need_review 治理候选，人审通过后经
    既有 import/promote 链路晋升为 governed memory。
    幂等：同一 task_id 只导出一次；已审核（非 need_review）的不再覆盖。
    """

    result = ExportResult()
    for row in _fetch_task_outcome_rows(harness_memory_db_path, limit):
        exported = export_task_outcome_candidate(
            record=_task_outcome_record_from_row(row),
            vault_path=vault_path,
            store=store,
            now=now,
            actor="knowledge-cycle-reconciliation",
        )
        if exported.status == "unchanged":
            result.skipped_count += 1
            continue
        result.exported_count += 1
        result.memory_ids.append(exported.memory_id)
        result.note_paths.append(exported.note_path)

    return result


def export_task_outcome_candidate(
    *,
    record: HarnessMemoryRecord,
    vault_path: Path | str,
    store: MemoryGovernanceStore,
    now: str,
    actor: str,
) -> TaskOutcomeCandidateExport:
    """Publish or reconcile one Harness task outcome review candidate.

    Args:
        record: Bounded Harness task-outcome memory record.
        vault_path: Obsidian vault used only as the review adapter.
        store: Authoritative governed-memory state store.
        now: ISO timestamp for state-changing writes.
        actor: Audit actor for candidate creation or recovery.

    Returns:
        Candidate publication status and stable identifiers.
    """

    store.initialize()
    vault_path = Path(vault_path)
    inbox_dir = vault_path / INBOX_DIR
    inbox_dir.mkdir(parents=True, exist_ok=True)
    desired = _memory_from_task_outcome_record(record, now=now)
    existing = store.get_memory(desired.memory_id)
    if existing is not None:
        existing_note_path = str(existing.obsidian_note_path or "").strip()
        note_path = (
            Path(existing_note_path)
            if existing_note_path
            else inbox_dir / f"{existing.memory_id}.md"
        )
        if existing.review_status != "need_review" or note_path.exists():
            return TaskOutcomeCandidateExport(
                memory_id=existing.memory_id,
                status="unchanged",
                note_path=note_path,
            )
        desired = apply_note_path(existing, str(note_path))
        status = "recovered"
        action = "distillation_candidate_recovered"
    else:
        note_path = inbox_dir / f"{desired.memory_id}.md"
        desired = apply_note_path(desired, str(note_path))
        status = "created"
        action = "distillation_candidate_created"

    temporary_path = note_path.with_suffix(f"{note_path.suffix}.tmp")
    temporary_path.write_text(render_governance_note(desired), encoding="utf-8")
    temporary_path.replace(note_path)
    store.upsert_memory(desired)
    store.append_audit(
        desired.memory_id,
        action,
        actor,
        {
            "source_system": desired.source_system,
            "source_id": desired.source_id,
            "source_hash": desired.source_hash,
            "review_status": desired.review_status,
            "note_path": str(note_path),
        },
        created_at=now,
    )
    return TaskOutcomeCandidateExport(
        memory_id=desired.memory_id,
        status=status,
        note_path=note_path,
    )


def stable_task_outcome_memory_id(task_id: str) -> str:
    """Deterministic governed memory id for a harness task outcome."""

    digest = hashlib.sha1(f"harness:task:{task_id}".encode()).hexdigest()
    return f"mem_task_{digest[:12]}"


def _fetch_task_outcome_rows(db_path: Path, limit: int) -> list[sqlite3.Row]:
    if not db_path.exists() or limit < 1:
        return []
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            """
            SELECT memory_id, session_id, conversation_id, task_id, domain,
                   memory_kind, title, summary, payload_json, created_at
            FROM harness_memories
            WHERE memory_kind = 'task_outcome'
              AND TRIM(COALESCE(summary, '')) != ''
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()


def _task_outcome_record_from_row(row: sqlite3.Row) -> HarnessMemoryRecord:
    return HarnessMemoryRecord(
        memory_id=str(row["memory_id"]),
        session_id=str(row["session_id"]),
        conversation_id=str(row["conversation_id"]),
        task_id=str(row["task_id"]),
        domain=str(row["domain"] or ""),
        memory_kind=str(row["memory_kind"]),
        title=str(row["title"] or ""),
        summary=str(row["summary"] or ""),
        payload={},
        created_at=str(row["created_at"]),
    )


def _memory_from_task_outcome_record(
    record: HarnessMemoryRecord,
    *,
    now: str,
) -> GovernedMemory:
    task_id = record.task_id
    summary = record.summary.strip()
    title = (record.title or f"任务结论 {task_id[:8]}").strip()
    domain = record.domain.strip()
    raw = json.dumps(
        {
            "task_id": task_id,
            "title": title,
            "summary": summary,
            "domain": domain,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    tags = _dedupe(
        [
            "harness-task",
            "task-outcome",
            *([f"domain:{domain}"] if domain else []),
        ]
    )
    return GovernedMemory(
        memory_id=stable_task_outcome_memory_id(task_id),
        source_system="harness",
        source_id=f"harness:task:{task_id}",
        source_path=f"harness:task:{task_id}",
        source_hash=f"sha256:{hashlib.sha256(raw.encode('utf-8')).hexdigest()}",
        title=title,
        summary=summary[:500],
        canonical_text=summary,
        memory_kind="fact",
        review_status="need_review",
        confidence=0.6,
        sensitivity="internal",
        owner=domain,
        project_id="",
        tags=tags,
        links=[],
        obsidian_note_path="",
        governance_version=1,
        created_at=record.created_at or now,
        updated_at=now,
        approved_at="",
        approved_by="",
    )


def stable_nas_memory_id(source_path: str, source_hash: str) -> str:
    """Build a deterministic governed memory id for a NAS source document."""

    digest = hashlib.sha1(f"{source_path}:{source_hash}".encode()).hexdigest()
    return f"mem_nas_{digest[:12]}"


def stable_content_sop_memory_hash(candidate: dict[str, Any]) -> str:
    """Build a deterministic source hash for a Content SOP memory candidate."""

    raw = json.dumps(
        {
            "candidate_id": str(candidate.get("candidate_id") or ""),
            "source_task_id": str(candidate.get("source_task_id") or ""),
            "canonical_text": str(candidate.get("canonical_text") or ""),
            "source_citations": candidate.get("source_citations") or [],
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return f"sha256:{hashlib.sha256(raw.encode('utf-8')).hexdigest()}"


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


def _memory_from_content_sop_candidate(
    candidate: dict[str, Any],
    *,
    now: str,
) -> GovernedMemory:
    candidate_id = str(candidate.get("candidate_id") or "").strip()
    canonical_text = str(candidate.get("canonical_text") or "").strip()
    if not candidate_id:
        raise ValueError("content SOP memory candidate requires candidate_id")
    if not canonical_text:
        raise ValueError("content SOP memory candidate requires canonical_text")

    department_id = str(candidate.get("department_id") or "").strip()
    scenario_id = str(candidate.get("scenario_id") or "").strip()
    source_task_id = str(candidate.get("source_task_id") or "").strip()
    tags = _dedupe(
        [
            "content-sop",
            "spiral-evolution",
            *([f"department_id:{department_id}"] if department_id else []),
            *([f"scenario_id:{scenario_id}"] if scenario_id else []),
            *([f"source_task_id:{source_task_id}"] if source_task_id else []),
        ]
    )
    source_hash = stable_content_sop_memory_hash(candidate)
    return GovernedMemory(
        memory_id=candidate_id,
        source_system="harness",
        source_id=f"harness:{source_task_id or candidate_id}",
        source_path=f"harness:{source_task_id or candidate_id}",
        source_hash=source_hash,
        title=_content_sop_memory_title(department_id, scenario_id),
        summary=canonical_text[:500],
        canonical_text=canonical_text,
        memory_kind=_memory_kind_from_candidate(
            str(candidate.get("memory_kind") or "")
        ),
        review_status="need_review",
        confidence=0.75,
        sensitivity="internal",
        owner="content_sop",
        project_id=scenario_id,
        tags=tags,
        links=_dedupe(_citation_links(candidate.get("source_citations"))),
        obsidian_note_path="",
        governance_version=1,
        created_at=now,
        updated_at=now,
        approved_at="",
        approved_by="",
    )


def _memory_kind_from_candidate(memory_kind: str) -> str:
    if memory_kind == "process_memory":
        return "process"
    if memory_kind == "preference_memory":
        return "preference"
    return "fact"


def _content_sop_memory_title(department_id: str, scenario_id: str) -> str:
    scope = " / ".join(item for item in (department_id, scenario_id) if item)
    return f"Content SOP memory candidate{f' - {scope}' if scope else ''}"


def _citation_links(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    links: list[str] = []
    for item in value:
        if isinstance(item, dict):
            link = str(item.get("source_path") or item.get("url") or "").strip()
        else:
            link = str(item or "").strip()
        if link:
            links.append(link)
    return links


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
