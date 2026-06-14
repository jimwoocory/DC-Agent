"""Handoff NAS Lark chat archives into reviewable distillation candidates."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dc_engines.assistant_distillation import AssistantDistillationStore
from dc_engines.memory_governance.models import GovernedMemory
from dc_engines.memory_governance.obsidian_codec import (
    apply_note_path,
    render_governance_note,
)
from dc_engines.memory_governance.store import MemoryGovernanceStore

GOVERNANCE_INBOX_DIR = Path("40_MemoryGovernance") / "Inbox"
LARK_ARCHIVE_RELATIVE_ROOT = Path("knowledge") / "Chat" / "Lark Chat"

_TONE_FEEDBACK_RE = re.compile(
    r"(生硬|不礼貌|太官方|像系统|不自然|冷冰冰|人性化|语气|敬语)"
)
_GROUP_HELP_RE = re.compile(r"(拉.{0,3}进群|进群聊|加群|拉群|群聊)")
_WRITING_ALIAS_RE = re.compile(r"(短一点|精简|太啰嗦|润色|优化.{0,6}(推文|文案|话术))")
_SENSITIVE_RE = re.compile(
    r"(手机号|电话|身份证|密码|token|密钥|工资|薪资|银行卡|账号|住址|地址)",
    re.IGNORECASE,
)


@dataclass(slots=True)
class LarkArchiveHandoffSummary:
    scanned_files: int = 0
    scanned_records: int = 0
    exported_memories: int = 0
    skipped_memories: int = 0
    assistant_candidates: int = 0
    memory_ids: list[str] = field(default_factory=list)
    note_paths: list[Path] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned_files": self.scanned_files,
            "scanned_records": self.scanned_records,
            "exported_memories": self.exported_memories,
            "skipped_memories": self.skipped_memories,
            "assistant_candidates": self.assistant_candidates,
            "memory_ids": self.memory_ids,
            "note_paths": [str(path) for path in self.note_paths],
        }


def export_lark_archive_handoff(
    *,
    archive_root: Path | str,
    distillation_store: AssistantDistillationStore,
    governance_store: MemoryGovernanceStore,
    vault_path: Path | str,
    limit_files: int = 200,
    min_feedback_count: int = 2,
    now: str | None = None,
) -> LarkArchiveHandoffSummary:
    """Export NAS Lark chat archives into pending review queues.

    This is deterministic by design: it does not summarize with an LLM and never
    promotes memories directly into runtime recall.
    """

    now = now or _now_iso()
    archive_root = Path(archive_root)
    vault_path = Path(vault_path)
    governance_store.initialize()

    summary = LarkArchiveHandoffSummary()
    grouped: dict[Path, list[dict[str, Any]]] = {}
    for jsonl_path in _iter_archive_jsonl_files(archive_root, limit_files=limit_files):
        records = _load_archive_records(jsonl_path)
        summary.scanned_files += 1
        summary.scanned_records += len(records)
        if records:
            grouped[jsonl_path] = records

    summary.assistant_candidates = _generate_assistant_candidates(
        grouped,
        store=distillation_store,
        min_feedback_count=min_feedback_count,
    )

    inbox_dir = vault_path / GOVERNANCE_INBOX_DIR
    inbox_dir.mkdir(parents=True, exist_ok=True)
    for jsonl_path, records in grouped.items():
        memory = _memory_from_lark_archive_file(jsonl_path, records, now=now)
        existing = governance_store.get_memory(memory.memory_id)
        if existing and existing.review_status != "need_review":
            summary.skipped_memories += 1
            continue
        note_path = inbox_dir / f"{memory.memory_id}.md"
        memory = apply_note_path(memory, str(note_path))
        note_path.write_text(render_governance_note(memory), encoding="utf-8")
        governance_store.upsert_memory(memory)
        governance_store.append_audit(
            memory.memory_id,
            "lark_archive_handoff_exported",
            "lark-archive-distillation",
            {"source_path": str(jsonl_path), "record_count": len(records)},
            created_at=now,
        )
        summary.exported_memories += 1
        summary.memory_ids.append(memory.memory_id)
        summary.note_paths.append(note_path)

    return summary


def _iter_archive_jsonl_files(
    archive_root: Path,
    *,
    limit_files: int,
) -> list[Path]:
    if not archive_root.exists() or limit_files < 1:
        return []
    root = archive_root
    if (root / LARK_ARCHIVE_RELATIVE_ROOT).exists():
        root = root / LARK_ARCHIVE_RELATIVE_ROOT
    return sorted(root.rglob("*.jsonl"), reverse=True)[:limit_files]


def _load_archive_records(jsonl_path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in jsonl_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and str(record.get("text") or "").strip():
            records.append(record)
    return records


def _generate_assistant_candidates(
    grouped_records: dict[Path, list[dict[str, Any]]],
    *,
    store: AssistantDistillationStore,
    min_feedback_count: int,
) -> int:
    tone_feedback: list[dict[str, Any]] = []
    group_help: list[dict[str, Any]] = []
    writing_alias: list[dict[str, Any]] = []

    for jsonl_path, records in grouped_records.items():
        for record in records:
            if record.get("sender_type") != "user":
                continue
            text = str(record.get("text") or "")
            evidence = _candidate_evidence(jsonl_path, record)
            if _TONE_FEEDBACK_RE.search(text):
                tone_feedback.append(evidence)
            if _GROUP_HELP_RE.search(text):
                group_help.append(evidence)
            if _WRITING_ALIAS_RE.search(text):
                writing_alias.append(evidence)

    created = 0
    if len(tone_feedback) >= min_feedback_count:
        store.upsert_candidate(
            kind="tone_template",
            template_name="lark_archive_employee_tone_feedback",
            template_body=(
                "回复员工反馈时，先承认体验问题，再用简短自然的语气说明处理动作，"
                "避免过度正式或系统化表达。"
            ),
            rationale="Repeated archived Lark chats mention assistant tone issues.",
            source="lark_archive_handoff",
            evidence=tone_feedback[:10],
            count=len(tone_feedback),
        )
        created += 1
    if len(group_help) >= min_feedback_count:
        store.upsert_candidate(
            kind="intent_alias",
            intent="writing",
            pattern="拉.{0,3}进群|进群聊|加群|拉群|群聊",
            rationale="Archived Lark chats show group-help requests in natural language.",
            source="lark_archive_handoff",
            evidence=group_help[:10],
            count=len(group_help),
        )
        created += 1
    if len(writing_alias) >= min_feedback_count:
        store.upsert_candidate(
            kind="intent_alias",
            intent="writing",
            pattern="短一点|精简|太啰嗦|润色|优化.{0,6}(推文|文案|话术)",
            rationale="Archived Lark chats show writing instructions in natural language.",
            source="lark_archive_handoff",
            evidence=writing_alias[:10],
            count=len(writing_alias),
        )
        created += 1
    return created


def _memory_from_lark_archive_file(
    jsonl_path: Path,
    records: list[dict[str, Any]],
    *,
    now: str,
) -> GovernedMemory:
    first = records[0]
    department = str(first.get("department") or "未知部门")
    employee_name = str(first.get("employee_name") or "未知员工")
    day = _archive_day(jsonl_path, first)
    source_hash = _source_hash(jsonl_path, records)
    canonical_text = _canonical_chat_digest(records)
    return GovernedMemory(
        memory_id=_stable_lark_memory_id(str(jsonl_path), source_hash),
        source_system="conversation",
        source_id=f"lark_archive:{jsonl_path}",
        source_path=str(jsonl_path),
        source_hash=source_hash,
        title=f"Lark Chat Digest - {department}/{employee_name}/{day}",
        summary=_summary_for_records(department, employee_name, day, records),
        canonical_text=canonical_text,
        memory_kind="fact",
        review_status="need_review",
        confidence=0.55,
        sensitivity=_sensitivity_for_records(records),
        owner=employee_name,
        project_id="",
        tags=[
            "lark-chat",
            "employee-memory",
            f"department:{department}",
            f"employee:{employee_name}",
        ],
        links=[str(jsonl_path)],
        obsidian_note_path="",
        governance_version=1,
        created_at=now,
        updated_at=now,
        approved_at="",
        approved_by="",
    )


def _canonical_chat_digest(records: list[dict[str, Any]], max_items: int = 24) -> str:
    lines: list[str] = []
    for record in records[:max_items]:
        sender_type = str(record.get("sender_type") or "unknown")
        timestamp = str(record.get("timestamp") or "")
        text = _compact_text(str(record.get("text") or ""), limit=500)
        lines.append(f"- {timestamp} [{sender_type}] {text}")
    if len(records) > max_items:
        lines.append(f"- ... {len(records) - max_items} more archived records omitted")
    return "\n".join(lines)


def _summary_for_records(
    department: str,
    employee_name: str,
    day: str,
    records: list[dict[str, Any]],
) -> str:
    user_count = sum(1 for record in records if record.get("sender_type") == "user")
    assistant_count = sum(
        1 for record in records if record.get("sender_type") == "assistant"
    )
    return (
        f"{day} Lark chat archive for {department}/{employee_name}: "
        f"{user_count} user messages and {assistant_count} assistant replies."
    )


def _candidate_evidence(jsonl_path: Path, record: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_path": str(jsonl_path),
        "timestamp": str(record.get("timestamp") or ""),
        "department": str(record.get("department") or ""),
        "employee_name": str(record.get("employee_name") or ""),
        "text": _compact_text(str(record.get("text") or ""), limit=240),
    }


def _source_hash(jsonl_path: Path, records: list[dict[str, Any]]) -> str:
    payload = json.dumps(records, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(f"{jsonl_path}\n{payload}".encode()).hexdigest()
    return f"sha256:{digest}"


def _stable_lark_memory_id(source_path: str, source_hash: str) -> str:
    digest = hashlib.sha1(f"{source_path}:{source_hash}".encode()).hexdigest()
    return f"mem_lark_{digest[:12]}"


def _archive_day(jsonl_path: Path, first: dict[str, Any]) -> str:
    try:
        return (
            datetime.fromisoformat(str(first.get("timestamp") or "")).date().isoformat()
        )
    except ValueError:
        return jsonl_path.stem


def _sensitivity_for_records(records: list[dict[str, Any]]) -> str:
    joined = "\n".join(str(record.get("text") or "") for record in records)
    return "confidential" if _SENSITIVE_RE.search(joined) else "internal"


def _compact_text(text: str, *, limit: int) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) <= limit:
        return compact
    return f"{compact[: limit - 1]}…"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
