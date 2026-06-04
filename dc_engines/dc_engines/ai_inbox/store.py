from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from .contracts import (
    INBOX_OPEN_STATUSES,
    InboxEvent,
    InboxItem,
    InboxItemCategory,
    InboxItemCreateRequest,
    InboxItemStatus,
)


class InboxStore:
    """SQLite store for employee-facing intake records.

    The inbox is intentionally separate from Harness. Harness tracks execution;
    this store tracks the communication object that came from an employee.
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
                CREATE TABLE IF NOT EXISTS inbox_items (
                    item_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    platform_id TEXT NOT NULL,
                    sender_id TEXT NOT NULL,
                    sender_name TEXT NOT NULL,
                    text TEXT NOT NULL,
                    category TEXT NOT NULL,
                    status TEXT NOT NULL,
                    case_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS inbox_events (
                    event_id TEXT PRIMARY KEY,
                    item_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_inbox_items_session
                ON inbox_items(session_id, updated_at DESC);

                CREATE INDEX IF NOT EXISTS idx_inbox_items_status
                ON inbox_items(status, updated_at DESC);

                CREATE INDEX IF NOT EXISTS idx_inbox_items_case
                ON inbox_items(case_id, updated_at DESC);

                CREATE INDEX IF NOT EXISTS idx_inbox_items_task
                ON inbox_items(task_id, updated_at DESC);

                CREATE INDEX IF NOT EXISTS idx_inbox_events_item
                ON inbox_events(item_id, created_at ASC);
            """)
            await db.commit()

        self._initialized = True

    async def create_item(
        self,
        request: InboxItemCreateRequest,
        *,
        item_id: str | None = None,
    ) -> InboxItem:
        await self.initialize()

        now = self._utcnow()
        item = InboxItem(
            item_id=item_id or uuid.uuid4().hex,
            session_id=request.session_id,
            conversation_id=request.conversation_id,
            platform_id=request.platform_id,
            sender_id=request.sender_id,
            sender_name=request.sender_name,
            text=request.text,
            category=request.category,
            status=request.status,
            case_id=request.case_id,
            task_id=request.task_id,
            source=request.source,
            payload=request.payload,
            created_at=now,
            updated_at=now,
        )

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO inbox_items (
                    item_id, session_id, conversation_id, platform_id,
                    sender_id, sender_name, text, category, status, case_id,
                    task_id, source, payload_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.item_id,
                    item.session_id,
                    item.conversation_id,
                    item.platform_id,
                    item.sender_id,
                    item.sender_name,
                    item.text,
                    item.category,
                    item.status,
                    item.case_id,
                    item.task_id,
                    item.source,
                    self._dumps(item.payload),
                    item.created_at,
                    item.updated_at,
                ),
            )
            await db.execute(
                """
                INSERT INTO inbox_events (
                    event_id, item_id, event_type, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    uuid.uuid4().hex,
                    item.item_id,
                    "item_created",
                    self._dumps(
                        {
                            "category": item.category,
                            "status": item.status,
                            "case_id": item.case_id,
                            "task_id": item.task_id,
                        }
                    ),
                    now,
                ),
            )
            await db.commit()

        return item

    async def get_item(self, item_id: str) -> InboxItem | None:
        await self.initialize()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM inbox_items WHERE item_id = ?",
                (item_id,),
            )
            row = await cursor.fetchone()
        return self._item_from_row(row) if row else None

    async def update_item(
        self,
        item_id: str,
        *,
        status: InboxItemStatus | None = None,
        case_id: str | None = None,
        task_id: str | None = None,
        payload_patch: dict[str, Any] | None = None,
        event_type: str = "item_updated",
        event_payload: dict[str, Any] | None = None,
    ) -> InboxItem:
        await self.initialize()
        existing = await self.get_item(item_id)
        if existing is None:
            raise LookupError(f"inbox item {item_id!r} not found")

        next_payload = dict(existing.payload)
        if payload_patch:
            next_payload.update(payload_patch)

        next_status = status or existing.status
        next_case_id = case_id if case_id is not None else existing.case_id
        next_task_id = task_id if task_id is not None else existing.task_id
        now = self._utcnow()
        payload = {
            "status": next_status,
            "case_id": next_case_id,
            "task_id": next_task_id,
            **(event_payload or {}),
        }

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE inbox_items
                SET status = ?, case_id = ?, task_id = ?,
                    payload_json = ?, updated_at = ?
                WHERE item_id = ?
                """,
                (
                    next_status,
                    next_case_id,
                    next_task_id,
                    self._dumps(next_payload),
                    now,
                    item_id,
                ),
            )
            await db.execute(
                """
                INSERT INTO inbox_events (
                    event_id, item_id, event_type, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (uuid.uuid4().hex, item_id, event_type, self._dumps(payload), now),
            )
            await db.commit()

        updated = await self.get_item(item_id)
        assert updated is not None
        return updated

    async def list_items(
        self,
        *,
        session_id: str | None = None,
        statuses: tuple[InboxItemStatus, ...] | None = None,
        categories: tuple[InboxItemCategory, ...] | None = None,
        limit: int = 50,
    ) -> list[InboxItem]:
        await self.initialize()
        query = "SELECT * FROM inbox_items WHERE 1 = 1"
        params: list[Any] = []
        if session_id:
            query += " AND session_id = ?"
            params.append(session_id)
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            query += f" AND status IN ({placeholders})"
            params.extend(statuses)
        if categories:
            placeholders = ", ".join("?" for _ in categories)
            query += f" AND category IN ({placeholders})"
            params.extend(categories)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(query, tuple(params))
            rows = await cursor.fetchall()
        return [self._item_from_row(row) for row in rows]

    async def find_latest_open_item(
        self,
        session_id: str,
        *,
        limit: int = 10,
    ) -> InboxItem | None:
        items = await self.list_items(
            session_id=session_id,
            statuses=tuple(sorted(INBOX_OPEN_STATUSES)),
            limit=limit,
        )
        return items[0] if items else None

    async def find_by_task_id(self, task_id: str) -> InboxItem | None:
        await self.initialize()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM inbox_items
                WHERE task_id = ?
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (task_id,),
            )
            row = await cursor.fetchone()
        return self._item_from_row(row) if row else None

    async def list_events(self, item_id: str) -> list[InboxEvent]:
        await self.initialize()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM inbox_events
                WHERE item_id = ?
                ORDER BY created_at ASC
                """,
                (item_id,),
            )
            rows = await cursor.fetchall()
        return [
            InboxEvent(
                event_id=row["event_id"],
                item_id=row["item_id"],
                event_type=row["event_type"],
                payload=json.loads(row["payload_json"] or "{}"),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    async def stats(self) -> dict[str, Any]:
        await self.initialize()
        async with aiosqlite.connect(self.db_path) as db:
            status_cursor = await db.execute(
                """
                SELECT status, count(*) AS count
                FROM inbox_items
                GROUP BY status
                ORDER BY count DESC
                """
            )
            status_rows = await status_cursor.fetchall()
            category_cursor = await db.execute(
                """
                SELECT category, count(*) AS count
                FROM inbox_items
                GROUP BY category
                ORDER BY count DESC
                """
            )
            category_rows = await category_cursor.fetchall()
        return {
            "status_counts": [
                {"status": row[0], "count": int(row[1])} for row in status_rows
            ],
            "category_counts": [
                {"category": row[0], "count": int(row[1])} for row in category_rows
            ],
        }

    def _item_from_row(self, row: aiosqlite.Row) -> InboxItem:
        return InboxItem(
            item_id=row["item_id"],
            session_id=row["session_id"],
            conversation_id=row["conversation_id"],
            platform_id=row["platform_id"],
            sender_id=row["sender_id"],
            sender_name=row["sender_name"],
            text=row["text"],
            category=row["category"],
            status=row["status"],
            case_id=row["case_id"],
            task_id=row["task_id"],
            source=row["source"],
            payload=json.loads(row["payload_json"] or "{}"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _dumps(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _utcnow() -> str:
        return datetime.now(timezone.utc).isoformat()
