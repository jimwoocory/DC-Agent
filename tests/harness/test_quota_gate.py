from __future__ import annotations

from pathlib import Path

import pytest

from harness.quota_gate import QuotaGate, QuotaRequest
from harness.resources import ResourceConfig
from harness.task_state import AdmissionMode, QueueStatus

RESOURCE_KEY = "unit_test_resource"


@pytest.fixture
def quota_gate(tmp_path: Path) -> QuotaGate:
    return QuotaGate(
        tmp_path / "quota.db",
        resource_configs={
            RESOURCE_KEY: ResourceConfig(
                key=RESOURCE_KEY,
                cooldown_after_completion_seconds=30,
                estimated_run_seconds=10,
            )
        },
    )


def _request(priority: int = 0) -> QuotaRequest:
    return QuotaRequest(
        primary_resource_key=RESOURCE_KEY,
        resource_keys=(RESOURCE_KEY,),
        payload={"kind": "unit"},
        requested_by="ou-user",
        session_id="session-1",
        priority=priority,
    )


async def _resource_row(gate: QuotaGate) -> dict:
    db = await gate.store.connect()
    try:
        cursor = await db.execute(
            "SELECT * FROM dc_llm_resource_state WHERE resource_key = ?",
            (RESOURCE_KEY,),
        )
        row = await cursor.fetchone()
        assert row is not None
        return dict(row)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_quota_gate_admits_first_request_run_now(
    quota_gate: QuotaGate,
) -> None:
    decision = await quota_gate.admit(_request())

    assert decision.mode is AdmissionMode.RUN_NOW
    assert decision.job.status is QueueStatus.RUNNING
    assert decision.job.payload == {"kind": "unit"}
    assert decision.job.requested_by == "ou-user"

    stored = await quota_gate.store.get_job(decision.job.job_id)
    assert stored is not None
    assert stored.status is QueueStatus.RUNNING

    resource = await _resource_row(quota_gate)
    assert resource["in_flight_job_id"] == decision.job.job_id


@pytest.mark.asyncio
async def test_quota_gate_queues_when_resource_is_in_flight(
    quota_gate: QuotaGate,
) -> None:
    await quota_gate.admit(_request())

    queued = await quota_gate.admit(_request())

    assert queued.mode is AdmissionMode.QUEUED
    assert queued.job.status is QueueStatus.PENDING
    assert queued.queue_position == 1
    assert queued.eta_at is not None
    assert queued.eta_at > queued.job.enqueue_at


@pytest.mark.asyncio
async def test_complete_releases_resource_to_cooldown(
    quota_gate: QuotaGate,
) -> None:
    admitted = await quota_gate.admit(_request())

    await quota_gate.complete(
        admitted.job.job_id,
        result={"summary": "done"},
        cooldown_seconds=15,
    )

    stored = await quota_gate.store.get_job(admitted.job.job_id)
    assert stored is not None
    assert stored.status is QueueStatus.COMPLETED
    assert stored.completed_at is not None

    resource = await _resource_row(quota_gate)
    assert resource["status"] == QueueStatus.COOLDOWN.value
    assert resource["in_flight_job_id"] is None
    assert resource["next_available_at"] is not None
    assert resource["last_success_at"] is not None


@pytest.mark.asyncio
async def test_cooldown_blocks_immediate_next_request(
    quota_gate: QuotaGate,
) -> None:
    admitted = await quota_gate.admit(_request())
    await quota_gate.complete(admitted.job.job_id, cooldown_seconds=60)

    queued = await quota_gate.admit(_request())

    assert queued.mode is AdmissionMode.QUEUED
    assert queued.reason == "Scarce resource is busy or cooling down."


@pytest.mark.asyncio
async def test_start_pending_job_runs_after_resource_is_available(
    quota_gate: QuotaGate,
) -> None:
    running = await quota_gate.admit(_request())
    queued = await quota_gate.admit(_request())
    await quota_gate.complete(running.job.job_id, cooldown_seconds=60)

    db = await quota_gate.store.connect()
    try:
        await db.execute(
            """
            UPDATE dc_llm_resource_state
            SET next_available_at = 0
            WHERE resource_key = ?
            """,
            (RESOURCE_KEY,),
        )
        await db.commit()
    finally:
        await db.close()

    started = await quota_gate.start_pending_job(queued.job.job_id)

    assert started is not None
    assert started.status is QueueStatus.RUNNING
    assert started.started_at is not None

    resource = await _resource_row(quota_gate)
    assert resource["in_flight_job_id"] == queued.job.job_id


@pytest.mark.asyncio
async def test_cancel_pending_job_does_not_release_running_resource(
    quota_gate: QuotaGate,
) -> None:
    running = await quota_gate.admit(_request())
    queued = await quota_gate.admit(_request())

    cancelled = await quota_gate.cancel_pending_job(
        queued.job.job_id, "no longer needed"
    )

    assert cancelled is True
    stored = await quota_gate.store.get_job(queued.job.job_id)
    assert stored is not None
    assert stored.status is QueueStatus.CANCELLED
    assert stored.error == "no longer needed"

    resource = await _resource_row(quota_gate)
    assert resource["in_flight_job_id"] == running.job.job_id


@pytest.mark.asyncio
async def test_fail_marks_job_failed_and_records_resource_error(
    quota_gate: QuotaGate,
) -> None:
    admitted = await quota_gate.admit(_request())

    await quota_gate.fail(
        admitted.job.job_id,
        "429 too many requests",
        retry_after_seconds=45,
    )

    stored = await quota_gate.store.get_job(admitted.job.job_id)
    assert stored is not None
    assert stored.status is QueueStatus.FAILED
    assert stored.error == "429 too many requests"

    resource = await _resource_row(quota_gate)
    assert resource["status"] == QueueStatus.COOLDOWN.value
    assert resource["last_error"] == "429 too many requests"
    assert resource["last_429_at"] is not None


@pytest.mark.asyncio
async def test_admit_rejects_empty_resource_key_set(
    quota_gate: QuotaGate,
) -> None:
    with pytest.raises(ValueError, match="resource_keys must not be empty"):
        await quota_gate.admit(
            QuotaRequest(
                primary_resource_key=RESOURCE_KEY,
                resource_keys=(),
            )
        )
