"""SQLite store for scarce-model queue state."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aiosqlite

from harness.resources import DEFAULT_RESOURCE_CONFIGS, ResourceConfig
from harness.task_state import QueueJob, QueueStatus


class QueueStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    async def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS dc_llm_queue_jobs (
                    job_id TEXT PRIMARY KEY,
                    primary_resource_key TEXT NOT NULL,
                    resource_keys_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 0,
                    requested_by TEXT,
                    session_id TEXT,
                    payload_json TEXT NOT NULL,
                    result_json TEXT,
                    enqueue_at REAL NOT NULL,
                    eta_at REAL,
                    lease_until REAL,
                    started_at REAL,
                    completed_at REAL,
                    error TEXT
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS dc_llm_resource_state (
                    resource_key TEXT PRIMARY KEY,
                    status TEXT NOT NULL DEFAULT 'available',
                    in_flight_job_id TEXT,
                    next_available_at REAL,
                    last_success_at REAL,
                    last_429_at REAL,
                    cooldown_seconds INTEGER NOT NULL,
                    last_error TEXT
                )
                """
            )
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_dc_llm_queue_jobs_pending
                ON dc_llm_queue_jobs (
                    primary_resource_key,
                    status,
                    priority DESC,
                    enqueue_at ASC
                )
                """
            )
            for config in DEFAULT_RESOURCE_CONFIGS.values():
                await self.ensure_resource(db, config)
            await db.commit()

    async def connect(self) -> aiosqlite.Connection:
        db = await aiosqlite.connect(self.db_path)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys=ON")
        return db

    async def ensure_resource(
        self,
        db: aiosqlite.Connection,
        config: ResourceConfig,
    ) -> None:
        await db.execute(
            """
            INSERT INTO dc_llm_resource_state (
                resource_key,
                cooldown_seconds
            )
            VALUES (?, ?)
            ON CONFLICT(resource_key) DO UPDATE SET
                cooldown_seconds = excluded.cooldown_seconds
            """,
            (config.key, config.cooldown_after_completion_seconds),
        )

    async def get_job(self, job_id: str) -> QueueJob | None:
        db = await self.connect()
        try:
            row = await self._fetch_job_row(db, job_id)
        finally:
            await db.close()
        if row is None:
            return None
        return self._job_from_row(row)

    async def _fetch_job_row(
        self,
        db: aiosqlite.Connection,
        job_id: str,
    ) -> aiosqlite.Row | None:
        cursor = await db.execute(
            "SELECT * FROM dc_llm_queue_jobs WHERE job_id = ?",
            (job_id,),
        )
        return await cursor.fetchone()

    def _job_from_row(self, row: aiosqlite.Row) -> QueueJob:
        return QueueJob(
            job_id=row["job_id"],
            primary_resource_key=row["primary_resource_key"],
            resource_keys=tuple(json.loads(row["resource_keys_json"])),
            status=QueueStatus(row["status"]),
            payload=json.loads(row["payload_json"]),
            requested_by=row["requested_by"],
            session_id=row["session_id"],
            priority=row["priority"],
            enqueue_at=row["enqueue_at"],
            eta_at=row["eta_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            error=row["error"],
        )


def to_json_payload(value: dict[str, Any] | None) -> str:
    return json.dumps(value or {}, ensure_ascii=False, separators=(",", ":"))
