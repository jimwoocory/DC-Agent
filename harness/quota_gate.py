"""Global quota gate for scarce OAuth-backed model resources."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiosqlite

from harness.queue_store import QueueStore, to_json_payload
from harness.resources import DEFAULT_RESOURCE_CONFIGS, ResourceConfig
from harness.task_state import (
    AdmissionDecision,
    AdmissionMode,
    QueueJob,
    QueueStatus,
)


@dataclass(frozen=True, slots=True)
class QuotaRequest:
    primary_resource_key: str
    resource_keys: tuple[str, ...]
    payload: dict[str, Any] = field(default_factory=dict)
    requested_by: str | None = None
    session_id: str | None = None
    priority: int = 0


class QuotaGate:
    def __init__(
        self,
        db_path: str | Path,
        resource_configs: dict[str, ResourceConfig] | None = None,
    ) -> None:
        self.store = QueueStore(db_path)
        self.resource_configs = resource_configs or DEFAULT_RESOURCE_CONFIGS

    async def admit(self, request: QuotaRequest) -> AdmissionDecision:
        await self.store.init()
        now = time.time()
        db = await self.store.connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            await self._ensure_request_resources(db, request)
            resource_rows = await self._fetch_resource_rows(db, request.resource_keys)
            available = self._resources_available(resource_rows, now)
            if available:
                job = await self._insert_job(
                    db,
                    request=request,
                    status=QueueStatus.RUNNING,
                    now=now,
                    eta_at=now,
                    started_at=now,
                )
                await self._mark_resources_running(
                    db, request.resource_keys, job.job_id
                )
                await db.commit()
                return AdmissionDecision(
                    mode=AdmissionMode.RUN_NOW,
                    job=job,
                    eta_at=now,
                    reason="Scarce resource is available now.",
                )

            queue_position = await self._queue_position(db, request)
            eta_at = self._estimate_eta(resource_rows, request, queue_position, now)
            job = await self._insert_job(
                db,
                request=request,
                status=QueueStatus.PENDING,
                now=now,
                eta_at=eta_at,
            )
            await db.commit()
            return AdmissionDecision(
                mode=AdmissionMode.QUEUED,
                job=job,
                queue_position=queue_position,
                eta_at=eta_at,
                reason="Scarce resource is busy or cooling down.",
            )
        finally:
            await db.close()

    async def complete(
        self,
        job_id: str,
        *,
        result: dict[str, Any] | None = None,
        cooldown_seconds: int | None = None,
    ) -> None:
        await self.store.init()
        now = time.time()
        db = await self.store.connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            row = await self.store._fetch_job_row(db, job_id)
            if row is None:
                await db.rollback()
                msg = f"Queue job not found: {job_id}"
                raise ValueError(msg)

            resource_keys = tuple(json.loads(row["resource_keys_json"]))
            await db.execute(
                """
                UPDATE dc_llm_queue_jobs
                SET status = ?,
                    result_json = ?,
                    completed_at = ?
                WHERE job_id = ?
                """,
                (
                    QueueStatus.COMPLETED.value,
                    to_json_payload(result),
                    now,
                    job_id,
                ),
            )
            await self._release_resources_to_cooldown(
                db,
                resource_keys,
                now=now,
                cooldown_seconds=cooldown_seconds,
                last_success_at=now,
                last_error=None,
            )
            await db.commit()
        finally:
            await db.close()

    async def start_pending_job(self, job_id: str) -> QueueJob | None:
        """Try to move one pending job into RUNNING when its resources are free."""
        await self.store.init()
        now = time.time()
        db = await self.store.connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            row = await self.store._fetch_job_row(db, job_id)
            if row is None or row["status"] != QueueStatus.PENDING.value:
                await db.rollback()
                return None

            resource_keys = tuple(json.loads(row["resource_keys_json"]))
            await self._ensure_request_resources(
                db,
                QuotaRequest(
                    primary_resource_key=row["primary_resource_key"],
                    resource_keys=resource_keys,
                ),
            )
            resource_rows = await self._fetch_resource_rows(db, resource_keys)
            if not self._resources_available(resource_rows, now):
                await db.rollback()
                return None

            await db.execute(
                """
                UPDATE dc_llm_queue_jobs
                SET status = ?,
                    started_at = ?,
                    eta_at = ?
                WHERE job_id = ?
                """,
                (QueueStatus.RUNNING.value, now, now, job_id),
            )
            await self._mark_resources_running(db, resource_keys, job_id)
            await db.commit()
            job = self.store._job_from_row(row)
            job.status = QueueStatus.RUNNING
            job.started_at = now
            job.eta_at = now
            return job
        finally:
            await db.close()

    async def list_pending_jobs(self, *, limit: int = 50) -> list[QueueJob]:
        """Return pending jobs ordered by priority and enqueue time."""
        await self.store.init()
        db = await self.store.connect()
        try:
            cursor = await db.execute(
                """
                SELECT *
                FROM dc_llm_queue_jobs
                WHERE status = ?
                ORDER BY priority DESC, enqueue_at ASC
                LIMIT ?
                """,
                (QueueStatus.PENDING.value, limit),
            )
            rows = await cursor.fetchall()
            return [self.store._job_from_row(row) for row in rows]
        finally:
            await db.close()

    async def cancel_pending_job(self, job_id: str, reason: str = "") -> bool:
        """Cancel a pending job without touching resource state.

        This is intentionally separate from fail(): a pending job never acquired
        the scarce resource, so cancelling it must not release or cool down
        resources that may still belong to another running job.
        """
        await self.store.init()
        now = time.time()
        db = await self.store.connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            row = await self.store._fetch_job_row(db, job_id)
            if row is None or row["status"] != QueueStatus.PENDING.value:
                await db.rollback()
                return False

            await db.execute(
                """
                UPDATE dc_llm_queue_jobs
                SET status = ?,
                    completed_at = ?,
                    error = ?
                WHERE job_id = ?
                """,
                (
                    QueueStatus.CANCELLED.value,
                    now,
                    reason or "cancelled",
                    job_id,
                ),
            )
            await db.commit()
            return True
        finally:
            await db.close()

    async def fail(
        self,
        job_id: str,
        error: str,
        *,
        retry_after_seconds: int | None = None,
    ) -> None:
        await self.store.init()
        now = time.time()
        db = await self.store.connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            row = await self.store._fetch_job_row(db, job_id)
            if row is None:
                await db.rollback()
                msg = f"Queue job not found: {job_id}"
                raise ValueError(msg)

            resource_keys = tuple(json.loads(row["resource_keys_json"]))
            await db.execute(
                """
                UPDATE dc_llm_queue_jobs
                SET status = ?,
                    completed_at = ?,
                    error = ?
                WHERE job_id = ?
                """,
                (QueueStatus.FAILED.value, now, error, job_id),
            )
            await self._release_resources_to_cooldown(
                db,
                resource_keys,
                now=now,
                cooldown_seconds=retry_after_seconds,
                last_success_at=None,
                last_error=error,
            )
            await db.commit()
        finally:
            await db.close()

    async def _ensure_request_resources(
        self,
        db: aiosqlite.Connection,
        request: QuotaRequest,
    ) -> None:
        for resource_key in request.resource_keys:
            config = self.resource_configs.get(
                resource_key,
                ResourceConfig(key=resource_key),
            )
            await self.store.ensure_resource(db, config)

    async def _fetch_resource_rows(
        self,
        db: aiosqlite.Connection,
        resource_keys: tuple[str, ...],
    ) -> list[aiosqlite.Row]:
        if not resource_keys:
            msg = "QuotaRequest.resource_keys must not be empty"
            raise ValueError(msg)
        placeholders = ",".join("?" for _ in resource_keys)
        cursor = await db.execute(
            f"""
            SELECT * FROM dc_llm_resource_state
            WHERE resource_key IN ({placeholders})
            """,
            resource_keys,
        )
        rows = await cursor.fetchall()
        if len(rows) != len(set(resource_keys)):
            missing = set(resource_keys) - {row["resource_key"] for row in rows}
            msg = f"Missing resource state rows: {sorted(missing)}"
            raise RuntimeError(msg)
        return list(rows)

    def _resources_available(
        self,
        rows: list[aiosqlite.Row],
        now: float,
    ) -> bool:
        for row in rows:
            if row["in_flight_job_id"]:
                return False
            next_available_at = row["next_available_at"]
            if next_available_at is not None and next_available_at > now:
                return False
        return True

    async def _insert_job(
        self,
        db: aiosqlite.Connection,
        *,
        request: QuotaRequest,
        status: QueueStatus,
        now: float,
        eta_at: float,
        started_at: float | None = None,
    ) -> QueueJob:
        job = QueueJob(
            job_id=str(uuid4()),
            primary_resource_key=request.primary_resource_key,
            resource_keys=request.resource_keys,
            status=status,
            payload=request.payload,
            requested_by=request.requested_by,
            session_id=request.session_id,
            priority=request.priority,
            enqueue_at=now,
            eta_at=eta_at,
            started_at=started_at,
        )
        await db.execute(
            """
            INSERT INTO dc_llm_queue_jobs (
                job_id,
                primary_resource_key,
                resource_keys_json,
                status,
                priority,
                requested_by,
                session_id,
                payload_json,
                enqueue_at,
                eta_at,
                started_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job.job_id,
                job.primary_resource_key,
                json.dumps(job.resource_keys, ensure_ascii=False),
                job.status.value,
                job.priority,
                job.requested_by,
                job.session_id,
                to_json_payload(job.payload),
                job.enqueue_at,
                job.eta_at,
                job.started_at,
            ),
        )
        return job

    async def _mark_resources_running(
        self,
        db: aiosqlite.Connection,
        resource_keys: tuple[str, ...],
        job_id: str,
    ) -> None:
        for resource_key in resource_keys:
            await db.execute(
                """
                UPDATE dc_llm_resource_state
                SET status = ?,
                    in_flight_job_id = ?,
                    next_available_at = NULL,
                    last_error = NULL
                WHERE resource_key = ?
                """,
                (QueueStatus.RUNNING.value, job_id, resource_key),
            )

    async def _release_resources_to_cooldown(
        self,
        db: aiosqlite.Connection,
        resource_keys: tuple[str, ...],
        *,
        now: float,
        cooldown_seconds: int | None,
        last_success_at: float | None,
        last_error: str | None,
    ) -> None:
        for resource_key in resource_keys:
            config = self.resource_configs.get(
                resource_key,
                ResourceConfig(key=resource_key),
            )
            cooldown = cooldown_seconds or config.cooldown_after_completion_seconds
            await db.execute(
                """
                UPDATE dc_llm_resource_state
                SET status = ?,
                    in_flight_job_id = NULL,
                    next_available_at = ?,
                    last_success_at = COALESCE(?, last_success_at),
                    last_429_at = CASE WHEN ? IS NULL THEN last_429_at ELSE ? END,
                    last_error = ?
                WHERE resource_key = ?
                """,
                (
                    QueueStatus.COOLDOWN.value,
                    now + cooldown,
                    last_success_at,
                    last_error,
                    now,
                    last_error,
                    resource_key,
                ),
            )

    async def _queue_position(
        self,
        db: aiosqlite.Connection,
        request: QuotaRequest,
    ) -> int:
        cursor = await db.execute(
            """
            SELECT COUNT(*) AS pending_count
            FROM dc_llm_queue_jobs
            WHERE primary_resource_key = ?
              AND status = ?
              AND priority >= ?
            """,
            (
                request.primary_resource_key,
                QueueStatus.PENDING.value,
                request.priority,
            ),
        )
        row = await cursor.fetchone()
        return int(row["pending_count"]) + 1

    def _estimate_eta(
        self,
        resource_rows: list[aiosqlite.Row],
        request: QuotaRequest,
        queue_position: int,
        now: float,
    ) -> float:
        base_available_at = now
        for row in resource_rows:
            config = self.resource_configs.get(
                row["resource_key"],
                ResourceConfig(key=row["resource_key"]),
            )
            if row["in_flight_job_id"]:
                candidate = (
                    now
                    + config.estimated_run_seconds
                    + config.cooldown_after_completion_seconds
                )
            else:
                candidate = row["next_available_at"] or now
            base_available_at = max(base_available_at, candidate)

        primary_config = self.resource_configs.get(
            request.primary_resource_key,
            ResourceConfig(key=request.primary_resource_key),
        )
        slot_seconds = (
            primary_config.estimated_run_seconds
            + primary_config.cooldown_after_completion_seconds
        )
        return base_available_at + max(queue_position - 1, 0) * slot_seconds
