from __future__ import annotations

from pathlib import Path

import pytest
from dc_engines.harness import (
    ExecutorSettlement,
    HarnessArtifactSpec,
    HarnessDeliveryReceipt,
    HarnessEngine,
    HarnessTaskCreateRequest,
    HarnessTaskStore,
)


async def _runtime(tmp_path: Path) -> tuple[HarnessTaskStore, HarnessEngine, object]:
    store = HarnessTaskStore(tmp_path / "harness.db")
    await store.initialize()
    engine = HarnessEngine(store)
    task = await engine.create_task(
        HarnessTaskCreateRequest(
            title="Executor task",
            conversation_id="oc_exec",
            platform_id="lark",
            session_id="umo-1",
            domain="advanced_executor",
            payload={},
        )
    )
    await engine.mark_in_progress(task.task_id)
    return store, engine, task


async def test_begin_is_idempotent(tmp_path: Path) -> None:
    _store, engine, task = await _runtime(tmp_path)
    settlement = ExecutorSettlement(engine)

    first = await settlement.begin(
        task_id=task.task_id,
        executor_kind="codex",
        capability="deep_reasoning",
        idempotency_key="codex:request-1:attempt-1",
        request_digest="digest-1",
    )
    replay = await settlement.begin(
        task_id=task.task_id,
        executor_kind="codex",
        capability="deep_reasoning",
        idempotency_key="codex:request-1:attempt-1",
        request_digest="digest-1",
    )

    assert replay.execution_id == first.execution_id


async def test_success_settlement_is_durable_and_idempotent(tmp_path: Path) -> None:
    store, engine, task = await _runtime(tmp_path)
    settlement = ExecutorSettlement(engine)
    execution = await settlement.begin(
        task_id=task.task_id,
        executor_kind="codex",
        capability="deep_reasoning",
        idempotency_key="codex:request-1:attempt-1",
        request_digest="digest-1",
    )
    delivery = HarnessDeliveryReceipt(mode="runtime_owned", reference_digest="om-1")

    first = await settlement.settle(
        execution_id=execution.execution_id,
        idempotency_key="settle:codex:request-1",
        outcome="review_required",
        result={"summary": "Result returned", "output_sha256": "sha-1"},
        delivery=delivery,
    )
    replay = await ExecutorSettlement(
        HarnessEngine(HarnessTaskStore(store.db_path))
    ).settle(
        execution_id=execution.execution_id,
        idempotency_key="settle:codex:request-1",
        outcome="review_required",
        result={"summary": "Result returned", "output_sha256": "sha-1"},
        delivery=delivery,
    )

    assert first.settlement_id == replay.settlement_id
    assert replay.status == "applied"
    assert (await store.get_task(task.task_id)).status == "review_required"

    with pytest.raises(RuntimeError, match="different settlement"):
        await settlement.settle(
            execution_id=execution.execution_id,
            idempotency_key="settle:codex:request-1",
            outcome="failed",
            result={"summary": "conflict"},
            delivery=delivery,
        )


async def test_completed_settlement_requires_delivery_confirmation(
    tmp_path: Path,
) -> None:
    _store, engine, task = await _runtime(tmp_path)
    settlement = ExecutorSettlement(engine)
    execution = await settlement.begin(
        task_id=task.task_id,
        executor_kind="hermes",
        capability="deep_workflow",
        idempotency_key="hermes:task-1:attempt-1",
        request_digest="digest-1",
    )

    with pytest.raises(ValueError, match="delivery"):
        await settlement.settle(
            execution_id=execution.execution_id,
            idempotency_key="settle:hermes:task-1",
            outcome="completed",
            result={"summary": "done"},
            delivery=HarnessDeliveryReceipt(
                mode="runtime_owned", reference_digest="callback-1"
            ),
        )


async def test_media_success_requires_artifact(tmp_path: Path) -> None:
    store, engine, task = await _runtime(tmp_path)
    context = await store.get_or_create_work_context(
        scope_key="media:lark:oc_exec:ou-1",
        platform_id="lark",
        conversation_id="oc_exec",
        subject_ref="ou-1",
        session_id="umo-1",
    )
    await store.link_task_to_context(
        task_id=task.task_id,
        context_id=context.context_id,
        relation_type="root",
    )
    settlement = ExecutorSettlement(engine)
    execution = await settlement.begin(
        task_id=task.task_id,
        executor_kind="media",
        capability="image_generation",
        idempotency_key="media:task-1:attempt-1",
        request_digest="digest-1",
    )

    with pytest.raises(ValueError, match="Artifact"):
        await settlement.settle(
            execution_id=execution.execution_id,
            idempotency_key="settle:media:task-1",
            outcome="completed",
            result={"summary": "image delivered"},
            delivery=HarnessDeliveryReceipt(
                mode="confirmed", reference_digest="delivery-1"
            ),
        )

    receipt = await settlement.settle(
        execution_id=execution.execution_id,
        idempotency_key="settle:media:task-1",
        outcome="completed",
        result={"summary": "image delivered"},
        delivery=HarnessDeliveryReceipt(
            mode="confirmed", reference_digest="delivery-1"
        ),
        artifact=HarnessArtifactSpec(
            context_id=context.context_id,
            artifact_kind="image",
            uri="/tmp/poster.png",
            mime_type="image/png",
        ),
    )

    assert receipt.artifact_id


async def test_reconcile_applies_pending_settlement_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, engine, task = await _runtime(tmp_path)
    settlement = ExecutorSettlement(engine)
    execution = await settlement.begin(
        task_id=task.task_id,
        executor_kind="hermes",
        capability="deep_workflow",
        idempotency_key="hermes:task-2:attempt-1",
        request_digest="digest-2",
    )

    async def crash_after_record(receipt):
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(settlement, "_apply", crash_after_record)
    with pytest.raises(RuntimeError, match="simulated crash"):
        await settlement.settle(
            execution_id=execution.execution_id,
            idempotency_key="settle:hermes:task-2",
            outcome="completed",
            result={"summary": "delivered"},
            delivery=HarnessDeliveryReceipt(
                mode="confirmed", reference_digest="delivery-2"
            ),
        )

    pending = await store.list_pending_execution_settlements()
    assert len(pending) == 1
    assert (await store.get_task(task.task_id)).status == "in_progress"

    restarted = ExecutorSettlement(HarnessEngine(HarnessTaskStore(store.db_path)))
    assert await restarted.reconcile() == 1
    assert (await store.get_task(task.task_id)).status == "completed"
    assert (await store.list_pending_execution_settlements()) == []
