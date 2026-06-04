from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite


@dataclass(slots=True)
class HarnessMemoryRecord:
    memory_id: str
    session_id: str
    conversation_id: str
    task_id: str
    domain: str
    memory_kind: str
    title: str
    summary: str
    payload: dict
    created_at: str


class HarnessMemoryStore:
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
                CREATE TABLE IF NOT EXISTS harness_memories (
                    memory_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    domain TEXT NOT NULL,
                    memory_kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_harness_memories_task_kind
                ON harness_memories(task_id, memory_kind);

                CREATE INDEX IF NOT EXISTS idx_harness_memories_session
                ON harness_memories(session_id, created_at DESC);
            """)
            await db.commit()

        self._initialized = True

    async def create_memory(
        self,
        *,
        session_id: str,
        conversation_id: str,
        task_id: str,
        domain: str,
        memory_kind: str,
        title: str,
        summary: str,
        payload: dict,
    ) -> HarnessMemoryRecord:
        await self.initialize()
        record = HarnessMemoryRecord(
            memory_id=uuid.uuid4().hex,
            session_id=session_id,
            conversation_id=conversation_id,
            task_id=task_id,
            domain=domain,
            memory_kind=memory_kind,
            title=title,
            summary=summary,
            payload=payload,
            created_at=self._utcnow(),
        )
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT OR IGNORE INTO harness_memories (
                    memory_id, session_id, conversation_id, task_id, domain,
                    memory_kind, title, summary, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.memory_id,
                    record.session_id,
                    record.conversation_id,
                    record.task_id,
                    record.domain,
                    record.memory_kind,
                    record.title,
                    record.summary,
                    json.dumps(record.payload, ensure_ascii=False, sort_keys=True),
                    record.created_at,
                ),
            )
            await db.commit()
        existing = await self.get_by_task(task_id, memory_kind)
        assert existing is not None
        return existing

    async def get_by_task(
        self,
        task_id: str,
        memory_kind: str,
    ) -> HarnessMemoryRecord | None:
        await self.initialize()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM harness_memories
                WHERE task_id = ? AND memory_kind = ?
                LIMIT 1
                """,
                (task_id, memory_kind),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return self._record_from_row(row)

    async def list_for_session(
        self,
        session_id: str,
        *,
        limit: int = 5,
    ) -> list[HarnessMemoryRecord]:
        await self.initialize()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM harness_memories
                WHERE session_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (session_id, limit),
            )
            rows = await cursor.fetchall()
        return [self._record_from_row(row) for row in rows]

    def _record_from_row(self, row: aiosqlite.Row) -> HarnessMemoryRecord:
        return HarnessMemoryRecord(
            memory_id=row["memory_id"],
            session_id=row["session_id"],
            conversation_id=row["conversation_id"],
            task_id=row["task_id"],
            domain=row["domain"],
            memory_kind=row["memory_kind"],
            title=row["title"],
            summary=row["summary"],
            payload=json.loads(row["payload_json"] or "{}"),
            created_at=row["created_at"],
        )

    @staticmethod
    def _utcnow() -> str:
        return datetime.now(timezone.utc).isoformat()
