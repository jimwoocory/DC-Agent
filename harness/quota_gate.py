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

DEFAULT_RUNNING_LEASE_SECONDS = 15 * 60


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
        running_lease_seconds: int = DEFAULT_RUNNING_LEASE_SECONDS,
    ) -> None:
        self.store = QueueStore(db_path)
        self.resource_configs = resource_configs or DEFAULT_RESOURCE_CONFIGS
        self.running_lease_seconds = max(int(running_lease_seconds), 1)

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

    async def resources_available_now(
        self,
        resource_keys: tuple[str, ...],
    ) -> bool:
        """Read-only availability snapshot for L3 route arbitration.

        Unlike :meth:`admit`, this never inserts a queue job; it only
        registers missing resource rows (idempotent) and reports whether
        all requested resources are free right now.
        """
        await self.store.init()
        now = time.time()
        db = await self.store.connect()
        try:
            for resource_key in resource_keys:
                config = self.resource_configs.get(
                    resource_key,
                    ResourceConfig(key=resource_key),
                )
                await self.store.ensure_resource(db, config)
            await db.commit()
            rows = await self._fetch_resource_rows(db, resource_keys)
            return self._resources_available(rows, now)
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
            await self._require_running_job(db, row, job_id)

            resource_keys = tuple(json.loads(row["resource_keys_json"]))
            await db.execute(
                """
                UPDATE dc_llm_queue_jobs
                SET status = ?,
                    result_json = ?,
                    completed_at = ?,
                    lease_until = NULL
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
                job_id,
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
                    eta_at = ?,
                    lease_until = ?
                WHERE job_id = ?
                """,
                (
                    QueueStatus.RUNNING.value,
                    now,
                    now,
                    now + self.running_lease_seconds,
                    job_id,
                ),
            )
            await self._mark_resources_running(db, resource_keys, job_id)
            await db.commit()
            job = self.store._job_from_row(row)
            job.status = QueueStatus.RUNNING
            job.started_at = now
            job.eta_at = now
            job.lease_until = now + self.running_lease_seconds
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

    async def heartbeat(self, job_id: str, *, lease_seconds: int | None = None) -> bool:
        """Extend the lease for a running job.

        Args:
            job_id: Queue job identifier.
            lease_seconds: Optional lease duration from the current time.

        Returns:
            True when a running job lease was extended.
        """
        await self.store.init()
        now = time.time()
        duration = max(int(lease_seconds or self.running_lease_seconds), 1)
        db = await self.store.connect()
        try:
            cursor = await db.execute(
                """
                UPDATE dc_llm_queue_jobs
                SET lease_until = MAX(COALESCE(lease_until, 0), ?)
                WHERE job_id = ? AND status = ?
                """,
                (now + duration, job_id, QueueStatus.RUNNING.value),
            )
            await db.commit()
            return cursor.rowcount == 1
        finally:
            await db.close()

    async def reap_expired_running_jobs(self, *, limit: int = 50) -> list[str]:
        """Fail expired running jobs and release their owned resources.

        Legacy running jobs with no lease are considered expired. Resource
        release is ownership-checked, so a stale job cannot release a resource
        that has already been reassigned.

        Args:
            limit: Maximum number of expired jobs to reclaim per scan.

        Returns:
            Reclaimed queue job identifiers.
        """
        await self.store.init()
        now = time.time()
        db = await self.store.connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """
                SELECT *
                FROM dc_llm_queue_jobs
                WHERE status = ?
                  AND (lease_until IS NULL OR lease_until <= ?)
                ORDER BY started_at ASC
                LIMIT ?
                """,
                (QueueStatus.RUNNING.value, now, limit),
            )
            rows = await cursor.fetchall()
            reclaimed: list[str] = []
            for row in rows:
                job_id = str(row["job_id"])
                await db.execute(
                    """
                    UPDATE dc_llm_queue_jobs
                    SET status = ?,
                        completed_at = ?,
                        lease_until = NULL,
                        error = ?
                    WHERE job_id = ? AND status = ?
                    """,
                    (
                        QueueStatus.FAILED.value,
                        now,
                        "running lease expired",
                        job_id,
                        QueueStatus.RUNNING.value,
                    ),
                )
                await self._release_resources_to_cooldown(
                    db,
                    tuple(json.loads(row["resource_keys_json"])),
                    job_id,
                    now=now,
                    cooldown_seconds=0,
                    last_success_at=None,
                    last_error="running lease expired",
                )
                reclaimed.append(job_id)
            await db.commit()
            return reclaimed
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
                    error = ?,
                    lease_until = NULL
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

    async def cancel_session_jobs(self, session_id: str, *, reason: str) -> list[str]:
        """Cancel pending and running queue jobs for one conversation session.

        Args:
            session_id: Unified message origin stored on queue admission.
            reason: Cancellation reason persisted with every matched job.

        Returns:
            Queue job identifiers moved to ``cancelled``.
        """
        await self.store.init()
        now = time.time()
        db = await self.store.connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """
                SELECT *
                FROM dc_llm_queue_jobs
                WHERE session_id = ? AND status IN (?, ?)
                ORDER BY enqueue_at ASC
                """,
                (
                    session_id,
                    QueueStatus.PENDING.value,
                    QueueStatus.RUNNING.value,
                ),
            )
            rows = await cursor.fetchall()
            cancelled: list[str] = []
            for row in rows:
                job_id = str(row["job_id"])
                await db.execute(
                    """
                    UPDATE dc_llm_queue_jobs
                    SET status = ?,
                        completed_at = ?,
                        lease_until = NULL,
                        error = ?
                    WHERE job_id = ? AND status IN (?, ?)
                    """,
                    (
                        QueueStatus.CANCELLED.value,
                        now,
                        reason or "cancelled",
                        job_id,
                        QueueStatus.PENDING.value,
                        QueueStatus.RUNNING.value,
                    ),
                )
                if row["status"] == QueueStatus.RUNNING.value:
                    await self._release_resources_to_cooldown(
                        db,
                        tuple(json.loads(row["resource_keys_json"])),
                        job_id,
                        now=now,
                        cooldown_seconds=0,
                        last_success_at=None,
                        last_error=reason or "cancelled",
                    )
                cancelled.append(job_id)
            await db.commit()
            return cancelled
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
            await self._require_running_job(db, row, job_id)

            resource_keys = tuple(json.loads(row["resource_keys_json"]))
            await db.execute(
                """
                UPDATE dc_llm_queue_jobs
                SET status = ?,
                    completed_at = ?,
                    error = ?,
                    lease_until = NULL
                WHERE job_id = ?
                """,
                (QueueStatus.FAILED.value, now, error, job_id),
            )
            await self._release_resources_to_cooldown(
                db,
                resource_keys,
                job_id,
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

    async def _require_running_job(
        self,
        db: aiosqlite.Connection,
        row: aiosqlite.Row,
        job_id: str,
    ) -> None:
        status = row["status"]
        if status == QueueStatus.RUNNING.value:
            return
        await db.rollback()
        msg = f"Queue job is not running: {job_id} (status={status})"
        raise ValueError(msg)

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
            lease_until=(
                now + self.running_lease_seconds
                if status is QueueStatus.RUNNING
                else None
            ),
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
                lease_until,
                started_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                job.lease_until,
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
        job_id: str,
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
            cooldown = (
                config.cooldown_after_completion_seconds
                if cooldown_seconds is None
                else cooldown_seconds
            )
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
                  AND in_flight_job_id = ?
                """,
                (
                    QueueStatus.COOLDOWN.value,
                    now + cooldown,
                    last_success_at,
                    last_error,
                    now,
                    last_error,
                    resource_key,
                    job_id,
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
