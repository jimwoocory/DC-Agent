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


async def _job_row(gate: QuotaGate, job_id: str) -> dict:
    db = await gate.store.connect()
    try:
        cursor = await db.execute(
            "SELECT * FROM dc_llm_queue_jobs WHERE job_id = ?",
            (job_id,),
        )
        row = await cursor.fetchone()
        assert row is not None
        return dict(row)
    finally:
        await db.close()


async def _set_job_status(gate: QuotaGate, job_id: str, status: str) -> None:
    db = await gate.store.connect()
    try:
        await db.execute(
            """
            UPDATE dc_llm_queue_jobs
            SET status = ?
            WHERE job_id = ?
            """,
            (status, job_id),
        )
        await db.commit()
    finally:
        await db.close()


async def _set_resource_in_flight(gate: QuotaGate, job_id: str) -> None:
    db = await gate.store.connect()
    try:
        await db.execute(
            """
            UPDATE dc_llm_resource_state
            SET status = ?,
                in_flight_job_id = ?,
                next_available_at = NULL,
                last_success_at = NULL,
                last_429_at = NULL,
                last_error = NULL
            WHERE resource_key = ?
            """,
            (QueueStatus.RUNNING.value, job_id, RESOURCE_KEY),
        )
        await db.commit()
    finally:
        await db.close()


async def _reject_release_call(*args: object, **kwargs: object) -> None:
    raise AssertionError("resource release should not be called")


async def _complete_or_fail(
    gate: QuotaGate,
    action: str,
    job_id: str,
) -> None:
    if action == "complete":
        await gate.complete(job_id, result={"summary": "done"})
        return
    await gate.fail(job_id, "should not terminalize")


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


@pytest.mark.parametrize("action", ["complete", "fail"])
@pytest.mark.asyncio
async def test_pending_job_complete_or_fail_rejects_without_releasing_resource(
    quota_gate: QuotaGate,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    running = await quota_gate.admit(_request())
    queued = await quota_gate.admit(_request())
    monkeypatch.setattr(
        quota_gate,
        "_release_resources_to_cooldown",
        _reject_release_call,
    )

    with pytest.raises(ValueError, match="not running"):
        await _complete_or_fail(quota_gate, action, queued.job.job_id)

    queued_row = await _job_row(quota_gate, queued.job.job_id)
    assert queued_row["status"] == QueueStatus.PENDING.value

    resource = await _resource_row(quota_gate)
    assert resource["status"] == QueueStatus.RUNNING.value
    assert resource["in_flight_job_id"] == running.job.job_id


@pytest.mark.parametrize("action", ["complete", "fail"])
@pytest.mark.parametrize(
    "status",
    [
        QueueStatus.CANCELLED.value,
        QueueStatus.FAILED.value,
        "unknown",
    ],
)
@pytest.mark.asyncio
async def test_non_running_terminal_or_unknown_jobs_reject_without_release(
    quota_gate: QuotaGate,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    status: str,
) -> None:
    admitted = await quota_gate.admit(_request())
    await _set_job_status(quota_gate, admitted.job.job_id, status)
    monkeypatch.setattr(
        quota_gate,
        "_release_resources_to_cooldown",
        _reject_release_call,
    )

    with pytest.raises(ValueError, match=f"status={status}"):
        await _complete_or_fail(quota_gate, action, admitted.job.job_id)

    stored = await _job_row(quota_gate, admitted.job.job_id)
    assert stored["status"] == status

    resource = await _resource_row(quota_gate)
    assert resource["status"] == QueueStatus.RUNNING.value
    assert resource["in_flight_job_id"] == admitted.job.job_id


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


@pytest.mark.parametrize("action", ["complete", "fail"])
@pytest.mark.asyncio
async def test_completed_job_duplicate_complete_or_fail_rejects_without_release(
    quota_gate: QuotaGate,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    admitted = await quota_gate.admit(_request())
    await quota_gate.complete(
        admitted.job.job_id,
        result={"summary": "done"},
        cooldown_seconds=15,
    )
    resource_before = await _resource_row(quota_gate)
    monkeypatch.setattr(
        quota_gate,
        "_release_resources_to_cooldown",
        _reject_release_call,
    )

    with pytest.raises(ValueError, match="status=completed"):
        await _complete_or_fail(quota_gate, action, admitted.job.job_id)

    stored = await quota_gate.store.get_job(admitted.job.job_id)
    assert stored is not None
    assert stored.status is QueueStatus.COMPLETED

    resource_after = await _resource_row(quota_gate)
    assert resource_after == resource_before


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


@pytest.mark.parametrize("action", ["complete", "fail"])
@pytest.mark.asyncio
async def test_running_job_complete_or_fail_only_releases_matching_in_flight_job(
    quota_gate: QuotaGate,
    action: str,
) -> None:
    admitted = await quota_gate.admit(_request())
    other_job_id = "other-running-job"
    await _set_resource_in_flight(quota_gate, other_job_id)

    await _complete_or_fail(quota_gate, action, admitted.job.job_id)

    resource = await _resource_row(quota_gate)
    assert resource["status"] == QueueStatus.RUNNING.value
    assert resource["in_flight_job_id"] == other_job_id


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
    assert resource["in_flight_job_id"] is None
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
