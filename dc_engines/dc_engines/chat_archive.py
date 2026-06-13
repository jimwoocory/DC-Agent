"""Pure Python Lark chat archive writer."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

SenderType = Literal["user", "assistant", "tool", "system"]

UNKNOWN_DEPARTMENT = "未知部门"
UNKNOWN_EMPLOYEE = "未知员工"


@dataclass(slots=True)
class LarkChatArchiveRecord:
    timestamp: str
    platform_id: str
    conversation_id: str
    message_id: str
    sender_type: SenderType
    feishu_open_id: str
    employee_name: str
    department: str
    text: str
    provider_model: str | None = None
    total_tokens: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class LarkChatArchiveResult:
    jsonl_path: Path
    markdown_path: Path
    text_hash: str


def append_lark_chat_record(
    root: Path | str, record: LarkChatArchiveRecord
) -> LarkChatArchiveResult:
    """Append one Lark chat record to date-partitioned JSONL and Markdown files."""
    root_path = Path(root)
    day = _record_day(record.timestamp)
    archive_dir = (
        root_path
        / "knowledge"
        / "Chat"
        / "Lark Chat"
        / _sanitize_segment(record.department, UNKNOWN_DEPARTMENT)
        / _sanitize_segment(record.employee_name, UNKNOWN_EMPLOYEE)
        / f"{day:%Y}"
        / f"{day:%m}"
    )
    archive_dir.mkdir(parents=True, exist_ok=True)

    jsonl_path = archive_dir / f"{day:%Y-%m-%d}.jsonl"
    markdown_path = archive_dir / f"{day:%Y-%m-%d}.md"
    text_hash = hashlib.sha256(record.text.encode("utf-8")).hexdigest()

    payload = asdict(record)
    payload["text_hash"] = text_hash
    json_line = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    with jsonl_path.open("a", encoding="utf-8") as jsonl_file:
        jsonl_file.write(json_line)
        jsonl_file.write("\n")

    with markdown_path.open("a", encoding="utf-8") as markdown_file:
        markdown_file.write(_markdown_section(record, text_hash))

    return LarkChatArchiveResult(
        jsonl_path=jsonl_path,
        markdown_path=markdown_path,
        text_hash=text_hash,
    )


def _sanitize_segment(value: str | None, fallback: str) -> str:
    segment = (value or "").strip()
    if not segment:
        return fallback
    return segment.replace("/", "／").replace("\\", "＼")


def _record_day(timestamp: str) -> datetime:
    normalized = timestamp.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    return datetime.fromisoformat(normalized)


def _markdown_section(record: LarkChatArchiveRecord, text_hash: str) -> str:
    employee_name = _sanitize_segment(record.employee_name, UNKNOWN_EMPLOYEE)
    department = _sanitize_segment(record.department, UNKNOWN_DEPARTMENT)
    lines = [
        f"## {record.timestamp} [{record.sender_type}]",
        "",
        f"- Platform: {record.platform_id}",
        f"- Conversation: {record.conversation_id}",
        f"- Message: {record.message_id}",
        f"- Feishu Open ID: {record.feishu_open_id}",
        f"- Employee: {employee_name}",
        f"- Department: {department}",
    ]
    if record.provider_model:
        lines.append(f"- Model: {record.provider_model}")
    if record.total_tokens is not None:
        lines.append(f"- Total Tokens: {record.total_tokens}")
    lines.extend(
        [
            f"- Text Hash: {text_hash}",
            "",
            record.text,
            "",
            "",
        ]
    )
    return "\n".join(lines)
