from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import aiosqlite

from dc_engines.case import CaseStore

BackfillAction = Literal["create", "update"]

ACTIVE_TASK_STATUSES = {"pending", "in_progress", "blocked", "review_required"}
TERMINAL_TASK_STATUSES = {"completed", "cancelled", "failed"}


@dataclass(slots=True)
class CaseBackfillGroup:
    backfill_key: str
    action: BackfillAction
    case_id: str
    session_id: str
    platform_id: str
    name: str
    status: str
    task_ids: list[str]
    domains: list[str]
    status_counts: dict[str, int]
    requester: dict[str, str] = field(default_factory=dict)
    existing_task_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "backfill_key": self.backfill_key,
            "action": self.action,
            "case_id": self.case_id,
            "session_id": self.session_id,
            "platform_id": self.platform_id,
            "name": self.name,
            "status": self.status,
            "task_ids": self.task_ids,
            "task_count": len(self.task_ids),
            "domains": self.domains,
            "status_counts": self.status_counts,
            "requester": self.requester,
            "existing_task_count": self.existing_task_count,
        }


@dataclass(slots=True)
class CaseBackfillReport:
    dry_run: bool
    generated_at: str
    planned_groups: list[CaseBackfillGroup]
    created: int = 0
    updated: int = 0
    skipped_tasks: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "generated_at": self.generated_at,
            "planned_group_count": len(self.planned_groups),
            "planned_task_count": sum(
                len(group.task_ids) for group in self.planned_groups
            ),
            "created": self.created,
            "updated": self.updated,
            "skipped_tasks": self.skipped_tasks,
            "groups": [group.to_dict() for group in self.planned_groups],
        }


class HistoricalCaseBackfiller:
    """Idempotently backfill Harness tasks into Case records."""

    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self.harness_db = self.data_dir / "harness.db"
        self.cases_db = self.data_dir / "cases.db"

    async def plan(self) -> CaseBackfillReport:
        tasks = await self._load_harness_tasks()
        existing = await self._load_existing_cases()
        attached_task_ids = existing["attached_task_ids"]
        existing_by_key = existing["by_backfill_key"]

        groups_by_session: dict[str, list[dict[str, Any]]] = {}
        skipped_tasks = 0
        for task in tasks:
            if task["task_id"] in attached_task_ids:
                skipped_tasks += 1
                continue
            groups_by_session.setdefault(task["session_id"], []).append(task)

        planned: list[CaseBackfillGroup] = []
        for session_id, group_tasks in groups_by_session.items():
            if not group_tasks:
                continue
            key = self._backfill_key(session_id)
            existing_case = existing_by_key.get(key)
            action: BackfillAction = "update" if existing_case else "create"
            existing_task_count = len(existing_case["task_ids"]) if existing_case else 0
            case_id = (
                existing_case["case_id"]
                if existing_case
                else f"hist_{hashlib.sha256(key.encode('utf-8')).hexdigest()[:24]}"
            )
            planned.append(
                self._build_group(
                    backfill_key=key,
                    action=action,
                    case_id=case_id,
                    session_id=session_id,
                    tasks=group_tasks,
                    existing_task_count=existing_task_count,
                )
            )

        planned.sort(key=lambda group: max(group.status_counts.values()), reverse=True)
        return CaseBackfillReport(
            dry_run=True,
            generated_at=self._utcnow(),
            planned_groups=planned,
            skipped_tasks=skipped_tasks,
        )

    async def apply(self) -> CaseBackfillReport:
        report = await self.plan()
        case_store = CaseStore(self.cases_db)
        await case_store.initialize()

        created = 0
        updated = 0
        for group in report.planned_groups:
            if group.action == "create":
                await self._create_case(case_store, group)
                created += 1
            else:
                await self._update_case(case_store, group)
                updated += 1

        return CaseBackfillReport(
            dry_run=False,
            generated_at=self._utcnow(),
            planned_groups=report.planned_groups,
            created=created,
            updated=updated,
            skipped_tasks=report.skipped_tasks,
        )

    async def _create_case(
        self,
        case_store: CaseStore,
        group: CaseBackfillGroup,
    ) -> None:
        payload = {
            "source": "company_cognition_backfill",
            "history_backfill_key": group.backfill_key,
            "auto_created": True,
            "task_count": len(group.task_ids),
            "domains": group.domains,
            "status_counts": group.status_counts,
            "backfilled_at": self._utcnow(),
            **group.requester,
        }
        case = await case_store.create_case(
            name=group.name,
            platform_id=group.platform_id,
            session_id=group.session_id,
            payload=payload,
            case_id=group.case_id,
        )
        await case_store.update_case_fields(
            case.case_id,
            status=group.status,  # type: ignore[arg-type]
            task_ids=group.task_ids,
            event_type="history_backfilled",
            event_payload={
                "source": "company_cognition_backfill",
                "task_ids": group.task_ids,
                "status": group.status,
            },
        )

    async def _update_case(
        self,
        case_store: CaseStore,
        group: CaseBackfillGroup,
    ) -> None:
        case = await case_store.get_case(group.case_id)
        if case is None:
            await self._create_case(case_store, group)
            return
        next_task_ids = [*case.task_ids]
        for task_id in group.task_ids:
            if task_id not in next_task_ids:
                next_task_ids.append(task_id)
        next_status = "drafting" if group.status != "archived" else case.status
        await case_store.update_case_fields(
            case.case_id,
            status=next_status,  # type: ignore[arg-type]
            task_ids=next_task_ids,
            event_type="history_backfill_updated",
            event_payload={
                "source": "company_cognition_backfill",
                "added_task_ids": group.task_ids,
                "status": next_status,
            },
        )

    def _build_group(
        self,
        *,
        backfill_key: str,
        action: BackfillAction,
        case_id: str,
        session_id: str,
        tasks: list[dict[str, Any]],
        existing_task_count: int,
    ) -> CaseBackfillGroup:
        tasks = sorted(tasks, key=lambda task: task["updated_at"], reverse=True)
        latest = tasks[0]
        domains = sorted({task["domain"] for task in tasks if task["domain"]})
        status_counts = dict(Counter(task["status"] for task in tasks))
        active_count = sum(
            count
            for status, count in status_counts.items()
            if status in ACTIVE_TASK_STATUSES
        )
        requester = self._requester_from_tasks(tasks)
        name = self._case_name(tasks, requester)
        return CaseBackfillGroup(
            backfill_key=backfill_key,
            action=action,
            case_id=case_id,
            session_id=session_id,
            platform_id=latest["platform_id"],
            name=name,
            status="drafting" if active_count else "archived",
            task_ids=[
                task["task_id"]
                for task in sorted(tasks, key=lambda item: item["created_at"])
            ],
            domains=domains,
            status_counts=status_counts,
            requester=requester,
            existing_task_count=existing_task_count,
        )

    def _requester_from_tasks(self, tasks: list[dict[str, Any]]) -> dict[str, str]:
        for task in tasks:
            payload = task["payload"]
            open_id = str(payload.get("requester_open_id") or "").strip()
            if not open_id:
                continue
            return {
                "requester_open_id": open_id,
                "requester_display_name": str(
                    payload.get("requester_display_name") or ""
                ),
                "requester_department": str(payload.get("requester_department") or ""),
                "requester_role": str(payload.get("requester_role") or ""),
            }
        return {}

    def _case_name(
        self,
        tasks: list[dict[str, Any]],
        requester: dict[str, str],
    ) -> str:
        representative = self._representative_task(tasks)
        requester_label = (
            requester.get("requester_display_name")
            or requester.get("requester_open_id", "")[:8]
            or self._session_label(representative["session_id"])
        )
        domain_label = self._domain_label(
            representative["domain"],
            representative["payload"],
        )
        title = self._clean_title(representative["title"])
        pieces = ["历史协作", requester_label]
        if domain_label:
            pieces.append(domain_label)
        if title:
            pieces.append(title[:32])
        return " · ".join(pieces)

    def _representative_task(self, tasks: list[dict[str, Any]]) -> dict[str, Any]:
        for task in tasks:
            title = str(task["title"] or "")
            if task["domain"] != "truth_intake" and "__card_action__" not in title:
                return task
        for task in tasks:
            if "__card_action__" not in str(task["title"] or ""):
                return task
        return tasks[0]

    def _domain_label(self, domain: str, payload: dict[str, Any]) -> str:
        department = str(payload.get("department") or "").strip()
        if department:
            return department
        if domain.startswith("department_workflow:"):
            return domain.split(":", 1)[1]
        return domain

    def _clean_title(self, title: str) -> str:
        text = " ".join(str(title or "").split())
        for prefix in ("部门工作流 |", "真实性资料校验：", "项目跟进 |", "营销策划 |"):
            if text.startswith(prefix):
                text = text[len(prefix) :].strip()
        return text

    def _session_label(self, session_id: str) -> str:
        if not session_id:
            return "unknown-session"
        return session_id.rsplit(":", 1)[-1][:8] or session_id[:8]

    async def _load_harness_tasks(self) -> list[dict[str, Any]]:
        if not self.harness_db.exists():
            return []
        async with aiosqlite.connect(self.harness_db) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA busy_timeout=1000")
            if not await self._table_exists(db, "harness_tasks"):
                return []
            cursor = await db.execute("""
                SELECT *
                FROM harness_tasks
                ORDER BY session_id ASC, updated_at ASC
            """)
            rows = await cursor.fetchall()
        return [
            {
                "task_id": row["task_id"],
                "conversation_id": row["conversation_id"],
                "platform_id": row["platform_id"],
                "session_id": row["session_id"],
                "title": row["title"],
                "domain": row["domain"],
                "status": row["status"],
                "payload": json.loads(row["payload_json"] or "{}"),
                "result": json.loads(row["result_json"] or "{}"),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    async def _load_existing_cases(self) -> dict[str, Any]:
        result = {"attached_task_ids": set(), "by_backfill_key": {}}
        if not self.cases_db.exists():
            return result
        async with aiosqlite.connect(self.cases_db) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA busy_timeout=1000")
            if not await self._table_exists(db, "cases"):
                return result
            cursor = await db.execute("SELECT * FROM cases")
            rows = await cursor.fetchall()
        for row in rows:
            payload = json.loads(row["payload_json"] or "{}")
            task_ids = json.loads(row["task_ids_json"] or "[]")
            result["attached_task_ids"].update(str(task_id) for task_id in task_ids)
            key = str(payload.get("history_backfill_key") or "")
            if key:
                result["by_backfill_key"][key] = {
                    "case_id": row["case_id"],
                    "task_ids": task_ids,
                }
        return result

    def _backfill_key(self, session_id: str) -> str:
        digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]
        return f"harness_session:{digest}"

    async def _table_exists(self, db: aiosqlite.Connection, table: str) -> bool:
        cursor = await db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        )
        return await cursor.fetchone() is not None

    @staticmethod
    def _utcnow() -> str:
        return datetime.now(timezone.utc).isoformat()
