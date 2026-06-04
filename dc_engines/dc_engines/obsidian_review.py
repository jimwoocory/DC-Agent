from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_DC_ROOT = Path("/Users/dianchi/DC-Agent")
DEFAULT_CONFIRMATIONS_PATH = (
    DEFAULT_DC_ROOT / "data" / "obsidian_review_confirmations.jsonl"
)


@dataclass(slots=True)
class ObsidianReviewCandidate:
    doc_key: str = ""
    title: str = ""
    rel_path: str = ""
    project_name: str = ""
    doc_type: str = ""
    owner: str = ""
    review_status: str = ""


@dataclass(slots=True)
class ObsidianReviewRecord:
    review_id: str
    created_at: str
    sender_id: str
    sender_name: str
    session_id: str
    platform_id: str
    raw_text: str
    action: str
    parsed_fields: dict[str, str] = field(default_factory=dict)
    candidates: list[ObsidianReviewCandidate] = field(default_factory=list)
    source: str = "ai_inbox_plugin"

    def to_dict(self) -> dict[str, Any]:
        return {
            "review_id": self.review_id,
            "created_at": self.created_at,
            "sender_id": self.sender_id,
            "sender_name": self.sender_name,
            "session_id": self.session_id,
            "platform_id": self.platform_id,
            "raw_text": self.raw_text,
            "action": self.action,
            "parsed_fields": self.parsed_fields,
            "candidates": [
                {
                    "doc_key": item.doc_key,
                    "title": item.title,
                    "rel_path": item.rel_path,
                    "project_name": item.project_name,
                    "doc_type": item.doc_type,
                    "owner": item.owner,
                    "review_status": item.review_status,
                }
                for item in self.candidates
            ],
            "source": self.source,
        }


CONFIRMATION_RE = re.compile(
    r"(确认无误|无误|没问题|需要调整|需要修改|负责人|项目归属|正确结果|文档类型)"
)


def looks_like_obsidian_review_reply(text: str) -> bool:
    normalized = _clean(text)
    if len(normalized) < 2:
        return False
    if not CONFIRMATION_RE.search(normalized):
        return False
    if "确认无误" in normalized or "需要调整" in normalized:
        return True
    return any(
        keyword in normalized
        for keyword in ("项目归属", "负责人", "文档类型", "正确结果")
    )


def parse_review_reply(text: str) -> tuple[str, dict[str, str]]:
    normalized = _clean(text)
    action = (
        "correction"
        if re.search(r"(需要调整|需要修改|纠正|不是|改成)", normalized)
        else "confirm"
    )
    fields: dict[str, str] = {}
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").splitlines():
        line = raw_line.strip().lstrip("-").strip()
        if not line or "：" not in line and ":" not in line:
            continue
        key, value = re.split(r"[:：]", line, maxsplit=1)
        key = key.strip()
        value = value.strip()
        if not key or not value:
            continue
        mapped = _field_name(key)
        if mapped:
            fields[mapped] = value[:500]
    return action, fields


def candidates_from_memory_context(
    context: dict[str, Any] | None, text: str = ""
) -> list[ObsidianReviewCandidate]:
    if not context:
        return []
    raw_docs = context.get("documents") or []
    candidates: list[ObsidianReviewCandidate] = []
    for doc in raw_docs:
        if not isinstance(doc, dict):
            continue
        status = str(doc.get("review_status") or "")
        if status and status != "need_review":
            continue
        candidate = ObsidianReviewCandidate(
            doc_key=str(doc.get("doc_key") or ""),
            title=str(doc.get("title") or ""),
            rel_path=str(doc.get("rel_path") or ""),
            project_name=str(doc.get("project_name") or ""),
            doc_type=str(doc.get("doc_type") or ""),
            owner=str(doc.get("owner") or ""),
            review_status=status or "need_review",
        )
        candidates.append(candidate)
    if text:
        candidates = _rank_candidates(candidates, text)
    return candidates[:5]


def build_review_record(
    *,
    text: str,
    sender_id: str,
    sender_name: str,
    session_id: str,
    platform_id: str,
    memory_context: dict[str, Any] | None = None,
) -> ObsidianReviewRecord:
    action, fields = parse_review_reply(text)
    return ObsidianReviewRecord(
        review_id=uuid.uuid4().hex,
        created_at=datetime.now(UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        sender_id=sender_id,
        sender_name=sender_name,
        session_id=session_id,
        platform_id=platform_id,
        raw_text=text[:4000],
        action=action,
        parsed_fields=fields,
        candidates=candidates_from_memory_context(memory_context, text),
    )


def append_review_record(
    record: ObsidianReviewRecord,
    path: Path = DEFAULT_CONFIRMATIONS_PATH,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(
            json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True) + "\n"
        )


def load_review_records(
    path: Path = DEFAULT_CONFIRMATIONS_PATH,
) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
    return records


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _field_name(key: str) -> str:
    if "项目" in key or "资料" in key:
        return "project_or_document"
    if "当前" in key:
        return "current_value"
    if "正确" in key:
        return "correct_value"
    if "负责人" in key or "owner" in key.lower():
        return "owner"
    if "部门" in key:
        return "department"
    if "文档类型" in key or "类型" == key:
        return "doc_type"
    if "备注" in key:
        return "note"
    return ""


def _rank_candidates(
    candidates: list[ObsidianReviewCandidate],
    text: str,
) -> list[ObsidianReviewCandidate]:
    normalized = _clean(text)

    def score(candidate: ObsidianReviewCandidate) -> int:
        value = 0
        for candidate_field in (
            candidate.title,
            candidate.project_name,
            candidate.rel_path,
            candidate.owner,
        ):
            if candidate_field and candidate_field in normalized:
                value += 10
        if candidate.owner:
            value += 1
        return value

    return sorted(candidates, key=score, reverse=True)
