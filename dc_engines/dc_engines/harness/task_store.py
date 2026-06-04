from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from .contracts import (
    HARNESS_TERMINAL_STATUSES,
    HarnessReviewDecision,
    HarnessTask,
    HarnessTaskCreateRequest,
    HarnessTaskEvent,
    HarnessTaskReview,
    HarnessTaskStatus,
)


class HarnessTaskStore:
    """SQLite sidecar store for Harness task traces.

    This store is intentionally narrow:

    - task metadata lives in ``harness_tasks``
    - append-only lifecycle records live in ``harness_task_events``
    - no direct coupling to provider or tool-execution internals yet
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
                CREATE TABLE IF NOT EXISTS harness_tasks (
                    task_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    platform_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    domain TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS harness_task_events (
                    event_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS harness_task_reviews (
                    review_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    reviewer_id TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    note TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_harness_tasks_conversation
                ON harness_tasks(conversation_id, updated_at DESC);

                CREATE INDEX IF NOT EXISTS idx_harness_task_events_task
                ON harness_task_events(task_id, created_at ASC);

                CREATE INDEX IF NOT EXISTS idx_harness_task_reviews_task
                ON harness_task_reviews(task_id, created_at ASC);
            """)
            await db.commit()

        self._initialized = True

    async def create_task(
        self,
        request: HarnessTaskCreateRequest,
        *,
        task_id: str | None = None,
    ) -> HarnessTask:
        await self.initialize()

        now = self._utcnow()
        task = HarnessTask(
            task_id=task_id or uuid.uuid4().hex,
            conversation_id=request.conversation_id,
            platform_id=request.platform_id,
            session_id=request.session_id,
            title=request.title,
            domain=request.domain,
            status="pending",
            payload=request.payload,
            result={},
            created_at=now,
            updated_at=now,
        )

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO harness_tasks (
                    task_id,
                    conversation_id,
                    platform_id,
                    session_id,
                    title,
                    domain,
                    status,
                    payload_json,
                    result_json,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.task_id,
                    task.conversation_id,
                    task.platform_id,
                    task.session_id,
                    task.title,
                    task.domain,
                    task.status,
                    json.dumps(task.payload, ensure_ascii=False, sort_keys=True),
                    json.dumps(task.result, ensure_ascii=False, sort_keys=True),
                    task.created_at,
                    task.updated_at,
                ),
            )
            await db.execute(
                """
                INSERT INTO harness_task_events (
                    event_id,
                    task_id,
                    event_type,
                    payload_json,
                    created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    uuid.uuid4().hex,
                    task.task_id,
                    "task_created",
                    json.dumps(
                        {
                            "title": task.title,
                            "domain": task.domain,
                            "status": task.status,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    now,
                ),
            )
            await db.commit()

        return task

    async def get_task(self, task_id: str) -> HarnessTask | None:
        await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM harness_tasks WHERE task_id = ?",
                (task_id,),
            )
            row = await cursor.fetchone()

        if row is None:
            return None
        return self._task_from_row(row)

    async def list_tasks_for_conversation(
        self,
        conversation_id: str,
        *,
        limit: int = 20,
    ) -> list[HarnessTask]:
        await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM harness_tasks
                WHERE conversation_id = ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (conversation_id, limit),
            )
            rows = await cursor.fetchall()

        return [self._task_from_row(row) for row in rows]

    async def list_tasks_for_session(
        self,
        session_id: str,
        *,
        limit: int = 20,
        statuses: tuple[HarnessTaskStatus, ...] | None = None,
    ) -> list[HarnessTask]:
        await self.initialize()

        query = """
            SELECT * FROM harness_tasks
            WHERE session_id = ?
        """
        params: list[object] = [session_id]
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

        return [self._task_from_row(row) for row in rows]

    async def get_latest_task_for_conversation(
        self,
        conversation_id: str,
        *,
        include_terminal: bool = False,
    ) -> HarnessTask | None:
        await self.initialize()

        query = """
            SELECT * FROM harness_tasks
            WHERE conversation_id = ?
        """
        params: list[object] = [conversation_id]
        if not include_terminal:
            placeholders = ", ".join("?" for _ in HARNESS_TERMINAL_STATUSES)
            query += f" AND status NOT IN ({placeholders})"
            params.extend(sorted(HARNESS_TERMINAL_STATUSES))
        query += " ORDER BY updated_at DESC LIMIT 1"

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(query, tuple(params))
            row = await cursor.fetchone()

        if row is None:
            return None
        return self._task_from_row(row)

    async def append_event(
        self,
        task_id: str,
        event_type: str,
        payload: dict,
    ) -> HarnessTaskEvent:
        await self.initialize()

        event = HarnessTaskEvent(
            event_id=uuid.uuid4().hex,
            task_id=task_id,
            event_type=event_type,
            payload=payload,
            created_at=self._utcnow(),
        )

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO harness_task_events (
                    event_id,
                    task_id,
                    event_type,
                    payload_json,
                    created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.task_id,
                    event.event_type,
                    json.dumps(event.payload, ensure_ascii=False, sort_keys=True),
                    event.created_at,
                ),
            )
            await db.commit()

        return event

    async def merge_task_payload(
        self,
        task_id: str,
        patch: dict,
        *,
        event_type: str = "payload_merged",
    ) -> HarnessTask:
        await self.initialize()

        existing = await self.get_task(task_id)
        if existing is None:
            raise LookupError(f"task {task_id!r} not found")

        now = self._utcnow()
        next_payload = {**existing.payload, **patch}

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE harness_tasks
                SET payload_json = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (
                    json.dumps(next_payload, ensure_ascii=False, sort_keys=True),
                    now,
                    task_id,
                ),
            )
            await db.execute(
                """
                INSERT INTO harness_task_events (
                    event_id,
                    task_id,
                    event_type,
                    payload_json,
                    created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    uuid.uuid4().hex,
                    task_id,
                    event_type,
                    json.dumps(patch, ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
            await db.commit()

        updated = await self.get_task(task_id)
        assert updated is not None
        return updated

    async def update_task_status(
        self,
        task_id: str,
        status: HarnessTaskStatus,
        *,
        result: dict | None = None,
        event_payload: dict | None = None,
    ) -> HarnessTask:
        await self.initialize()

        existing = await self.get_task(task_id)
        if existing is None:
            raise LookupError(f"task {task_id!r} not found")

        now = self._utcnow()
        next_result = result if result is not None else existing.result

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE harness_tasks
                SET status = ?, result_json = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (
                    status,
                    json.dumps(next_result, ensure_ascii=False, sort_keys=True),
                    now,
                    task_id,
                ),
            )
            await db.execute(
                """
                INSERT INTO harness_task_events (
                    event_id,
                    task_id,
                    event_type,
                    payload_json,
                    created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    uuid.uuid4().hex,
                    task_id,
                    "status_changed",
                    json.dumps(
                        {
                            "status": status,
                            **(event_payload or {}),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    now,
                ),
            )
            await db.commit()

        updated = await self.get_task(task_id)
        assert updated is not None
        return updated

    async def list_events(
        self,
        task_id: str,
    ) -> list[HarnessTaskEvent]:
        await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM harness_task_events
                WHERE task_id = ?
                ORDER BY created_at ASC
                """,
                (task_id,),
            )
            rows = await cursor.fetchall()

        return [
            HarnessTaskEvent(
                event_id=row["event_id"],
                task_id=row["task_id"],
                event_type=row["event_type"],
                payload=json.loads(row["payload_json"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    async def create_review(
        self,
        task_id: str,
        reviewer_id: str,
        decision: HarnessReviewDecision,
        note: str,
    ) -> HarnessTaskReview:
        await self.initialize()

        review = HarnessTaskReview(
            review_id=uuid.uuid4().hex,
            task_id=task_id,
            reviewer_id=reviewer_id,
            decision=decision,
            note=note,
            created_at=self._utcnow(),
        )

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO harness_task_reviews (
                    review_id,
                    task_id,
                    reviewer_id,
                    decision,
                    note,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    review.review_id,
                    review.task_id,
                    review.reviewer_id,
                    review.decision,
                    review.note,
                    review.created_at,
                ),
            )
            await db.commit()

        return review

    async def list_reviews(
        self,
        task_id: str,
    ) -> list[HarnessTaskReview]:
        await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM harness_task_reviews
                WHERE task_id = ?
                ORDER BY created_at ASC
                """,
                (task_id,),
            )
            rows = await cursor.fetchall()

        return [
            HarnessTaskReview(
                review_id=row["review_id"],
                task_id=row["task_id"],
                reviewer_id=row["reviewer_id"],
                decision=row["decision"],
                note=row["note"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def _task_from_row(self, row: aiosqlite.Row) -> HarnessTask:
        return HarnessTask(
            task_id=row["task_id"],
            conversation_id=row["conversation_id"],
            platform_id=row["platform_id"],
            session_id=row["session_id"],
            title=row["title"],
            domain=row["domain"],
            status=row["status"],
            payload=json.loads(row["payload_json"]),
            result=json.loads(row["result_json"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _utcnow(self) -> str:
        return datetime.now(timezone.utc).isoformat()
