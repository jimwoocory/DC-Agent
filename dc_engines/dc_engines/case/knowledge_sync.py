from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from .contracts import Case

_UNSAFE_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


@dataclass(slots=True)
class CaseKnowledgeSyncRecord:
    case_id: str
    status: str
    archive_path: str
    source_path: str
    task_ids: list[str]
    deliverable_count: int
    error: str | None
    created_at: str

    @classmethod
    def from_dict(cls, data: dict) -> CaseKnowledgeSyncRecord:
        return cls(
            case_id=str(data["case_id"]),
            status=str(data["status"]),
            archive_path=str(data.get("archive_path") or ""),
            source_path=str(data.get("source_path") or ""),
            task_ids=[str(task_id) for task_id in data.get("task_ids") or []],
            deliverable_count=int(data.get("deliverable_count") or 0),
            error=str(data["error"]) if data.get("error") is not None else None,
            created_at=str(data.get("created_at") or ""),
        )


class CaseKnowledgeSync:
    """Persist archived Case summaries for downstream knowledge ingestion."""

    def __init__(
        self,
        archive_root: str | Path = Path("data") / "case_archives",
        *,
        records_path: str | Path | None = None,
    ) -> None:
        self.archive_root = Path(archive_root)
        self.records_path = (
            Path(records_path)
            if records_path is not None
            else self.archive_root / "sync_records.jsonl"
        )

    def sync(
        self,
        case: Case,
        *,
        source_path: str | Path = "",
    ) -> CaseKnowledgeSyncRecord:
        created_at = self._utcnow()
        archive_path = self.archive_root / self._archive_filename(case.case_id)
        try:
            self._write_archive_markdown(case, archive_path, created_at)
        except Exception as exc:  # noqa: BLE001
            record = self._build_record(
                case,
                status="failed",
                archive_path=archive_path,
                source_path=source_path,
                error=str(exc),
                created_at=created_at,
            )
            self._append_record(record)
            return record

        record = self._build_record(
            case,
            status="synced",
            archive_path=archive_path,
            source_path=source_path,
            error=None,
            created_at=created_at,
        )
        self._append_record(record)
        return record

    def list_records(self) -> list[CaseKnowledgeSyncRecord]:
        if not self.records_path.exists():
            return []
        records: list[CaseKnowledgeSyncRecord] = []
        for line in self.records_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                data = json.loads(line)
                if isinstance(data, dict):
                    records.append(CaseKnowledgeSyncRecord.from_dict(data))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
        return records

    def latest_record_for_case(
        self,
        case_id_or_prefix: str,
    ) -> CaseKnowledgeSyncRecord | None:
        needle = case_id_or_prefix.strip()
        if not needle:
            return None
        matches = [
            record
            for record in self.list_records()
            if record.case_id.startswith(needle)
        ]
        matched_case_ids = {record.case_id for record in matches}
        if len(matched_case_ids) != 1:
            return None
        return matches[-1] if matches else None

    def _write_archive_markdown(
        self,
        case: Case,
        archive_path: Path,
        generated_at: str,
    ) -> None:
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        archive_path.write_text(
            self._render_markdown(case, generated_at),
            encoding="utf-8",
        )

    def _append_record(self, record: CaseKnowledgeSyncRecord) -> None:
        self.records_path.parent.mkdir(parents=True, exist_ok=True)
        with self.records_path.open("a", encoding="utf-8") as records_file:
            records_file.write(
                json.dumps(asdict(record), ensure_ascii=False, sort_keys=True) + "\n"
            )

    def _build_record(
        self,
        case: Case,
        *,
        status: str,
        archive_path: Path,
        source_path: str | Path,
        error: str | None,
        created_at: str,
    ) -> CaseKnowledgeSyncRecord:
        return CaseKnowledgeSyncRecord(
            case_id=case.case_id,
            status=status,
            archive_path=str(archive_path),
            source_path=str(Path(source_path)) if source_path else "",
            task_ids=list(case.task_ids),
            deliverable_count=len(case.deliverables),
            error=error,
            created_at=created_at,
        )

    def _render_markdown(self, case: Case, generated_at: str) -> str:
        deliverables_json = json.dumps(
            case.deliverables,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        payload_json = json.dumps(
            case.payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        task_ids = "\n".join(f"- {task_id}" for task_id in case.task_ids) or "- None"

        return "\n".join(
            [
                f"# {case.name}",
                "",
                f"- case_id: {case.case_id}",
                f"- client_name: {case.client_name or ''}",
                f"- platform_id: {case.platform_id}",
                f"- session_id: {case.session_id}",
                f"- generated_at: {generated_at}",
                "",
                "## Task IDs",
                "",
                task_ids,
                "",
                "## Deliverables",
                "",
                "```json",
                deliverables_json,
                "```",
                "",
                "## Payload",
                "",
                "```json",
                payload_json,
                "```",
                "",
            ]
        )

    @staticmethod
    def _utcnow() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _archive_filename(case_id: str) -> str:
        name = _UNSAFE_FILENAME_CHARS.sub("_", case_id.strip())
        name = name.replace("..", "_").strip(" ._")
        return f"{name or 'case'}.md"
