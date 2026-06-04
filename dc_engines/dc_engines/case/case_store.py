from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from .contracts import (
    CASE_ACTIVE_STATUSES,
    Case,
    CaseEvent,
    CaseStatus,
)


class CaseStore:
    """SQLite sidecar store for the Case aggregation layer.

    Two tables:
    - ``cases`` — one row per case with denormalized JSON columns for
      ``task_ids`` / ``deliverables`` / ``roles`` so consumers don't need
      additional joins for the common read path.
    - ``case_events`` — append-only audit trail keyed on case_id.
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        self._initialized = False

    async def initialize(self) -> None:
        if self._initialized:
            return

        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA synchronous=NORMAL")
            await db.executescript("""
                CREATE TABLE IF NOT EXISTS cases (
                    case_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    client_name TEXT,
                    platform_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    task_ids_json TEXT NOT NULL,
                    deliverables_json TEXT NOT NULL,
                    roles_json TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS case_events (
                    event_id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_cases_session
                ON cases(session_id, updated_at DESC);

                CREATE INDEX IF NOT EXISTS idx_cases_status
                ON cases(status, updated_at DESC);

                CREATE INDEX IF NOT EXISTS idx_case_events_case
                ON case_events(case_id, created_at ASC);
            """)
            await db.commit()

        self._initialized = True

    async def create_case(
        self,
        *,
        name: str,
        platform_id: str,
        session_id: str,
        client_name: str | None = None,
        payload: dict[str, Any] | None = None,
        case_id: str | None = None,
    ) -> Case:
        await self.initialize()

        now = self._utcnow()
        case = Case(
            case_id=case_id or uuid.uuid4().hex,
            name=name,
            status="initiated",
            client_name=client_name,
            platform_id=platform_id,
            session_id=session_id,
            task_ids=[],
            deliverables=[],
            roles={},
            version=1,
            created_at=now,
            updated_at=now,
            payload=dict(payload or {}),
        )

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO cases (
                    case_id,
                    name,
                    status,
                    client_name,
                    platform_id,
                    session_id,
                    task_ids_json,
                    deliverables_json,
                    roles_json,
                    version,
                    created_at,
                    updated_at,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    case.case_id,
                    case.name,
                    case.status,
                    case.client_name,
                    case.platform_id,
                    case.session_id,
                    self._dumps(case.task_ids),
                    self._dumps(case.deliverables),
                    self._dumps(case.roles),
                    case.version,
                    case.created_at,
                    case.updated_at,
                    self._dumps(case.payload),
                ),
            )
            await db.execute(
                """
                INSERT INTO case_events (
                    event_id, case_id, event_type, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    uuid.uuid4().hex,
                    case.case_id,
                    "case_created",
                    self._dumps(
                        {
                            "name": case.name,
                            "client_name": case.client_name,
                            "platform_id": case.platform_id,
                            "session_id": case.session_id,
                        }
                    ),
                    now,
                ),
            )
            await db.commit()

        return case

    async def get_case(self, case_id: str) -> Case | None:
        await self.initialize()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM cases WHERE case_id = ?",
                (case_id,),
            )
            row = await cursor.fetchone()
        return self._case_from_row(row) if row else None

    async def list_cases_for_session(
        self,
        session_id: str,
        *,
        limit: int = 10,
        statuses: tuple[CaseStatus, ...] | None = None,
    ) -> list[Case]:
        await self.initialize()
        query = "SELECT * FROM cases WHERE session_id = ?"
        params: list[Any] = [session_id]
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            query += f" AND status IN ({placeholders})"
            params.extend(statuses)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(query, tuple(params))
            rows = await cursor.fetchall()
        return [self._case_from_row(row) for row in rows]

    async def get_active_case_for_session(
        self,
        session_id: str,
    ) -> Case | None:
        """Find the most recently updated non-terminal case for a session."""
        await self.initialize()
        active = tuple(sorted(CASE_ACTIVE_STATUSES))
        placeholders = ", ".join("?" for _ in active)
        query = (
            f"SELECT * FROM cases WHERE session_id = ? AND status IN ({placeholders})"
            " ORDER BY updated_at DESC LIMIT 1"
        )
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(query, (session_id, *active))
            row = await cursor.fetchone()
        return self._case_from_row(row) if row else None

    async def update_case_fields(
        self,
        case_id: str,
        *,
        status: CaseStatus | None = None,
        task_ids: list[str] | None = None,
        deliverables: list[dict[str, Any]] | None = None,
        roles: dict[str, str] | None = None,
        version: int | None = None,
        event_type: str,
        event_payload: dict[str, Any],
    ) -> Case:
        await self.initialize()
        existing = await self.get_case(case_id)
        if existing is None:
            raise LookupError(f"case {case_id!r} not found")

        next_status = status if status is not None else existing.status
        next_task_ids = task_ids if task_ids is not None else existing.task_ids
        next_deliverables = (
            deliverables if deliverables is not None else existing.deliverables
        )
        next_roles = roles if roles is not None else existing.roles
        next_version = version if version is not None else existing.version
        now = self._utcnow()

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE cases SET
                    status = ?,
                    task_ids_json = ?,
                    deliverables_json = ?,
                    roles_json = ?,
                    version = ?,
                    updated_at = ?
                WHERE case_id = ?
                """,
                (
                    next_status,
                    self._dumps(next_task_ids),
                    self._dumps(next_deliverables),
                    self._dumps(next_roles),
                    next_version,
                    now,
                    case_id,
                ),
            )
            await db.execute(
                """
                INSERT INTO case_events (
                    event_id, case_id, event_type, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    uuid.uuid4().hex,
                    case_id,
                    event_type,
                    self._dumps(event_payload),
                    now,
                ),
            )
            await db.commit()

        updated = await self.get_case(case_id)
        assert updated is not None
        return updated

    async def append_event(
        self,
        case_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> CaseEvent:
        await self.initialize()
        event = CaseEvent(
            event_id=uuid.uuid4().hex,
            case_id=case_id,
            event_type=event_type,
            payload=payload,
            created_at=self._utcnow(),
        )
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO case_events (
                    event_id, case_id, event_type, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.case_id,
                    event.event_type,
                    self._dumps(event.payload),
                    event.created_at,
                ),
            )
            await db.commit()
        return event

    async def list_events(self, case_id: str) -> list[CaseEvent]:
        await self.initialize()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM case_events WHERE case_id = ?
                ORDER BY created_at ASC
                """,
                (case_id,),
            )
            rows = await cursor.fetchall()
        return [
            CaseEvent(
                event_id=row["event_id"],
                case_id=row["case_id"],
                event_type=row["event_type"],
                payload=json.loads(row["payload_json"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def _case_from_row(self, row: aiosqlite.Row) -> Case:
        return Case(
            case_id=row["case_id"],
            name=row["name"],
            status=row["status"],
            client_name=row["client_name"],
            platform_id=row["platform_id"],
            session_id=row["session_id"],
            task_ids=json.loads(row["task_ids_json"]),
            deliverables=json.loads(row["deliverables_json"]),
            roles=json.loads(row["roles_json"]),
            version=int(row["version"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            payload=json.loads(row["payload_json"]),
        )

    @staticmethod
    def _dumps(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _utcnow() -> str:
        return datetime.now(timezone.utc).isoformat()
