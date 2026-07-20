from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict

from .contracts import (
    HARNESS_TERMINAL_STATUSES,
    HarnessArtifactSpec,
    HarnessDeliveryReceipt,
    HarnessExecution,
    HarnessExecutionSettlement,
    HarnessSettlementOutcome,
)
from .engine import HarnessEngine

logger = logging.getLogger(__name__)


class ExecutorSettlement:
    """Close executor attempts through one durable Harness Interface."""

    def __init__(self, engine: HarnessEngine) -> None:
        self.engine = engine
        self.store = engine.store

    async def begin(
        self,
        *,
        task_id: str,
        executor_kind: str,
        capability: str,
        idempotency_key: str,
        request_digest: str,
        metadata: dict | None = None,
    ) -> HarnessExecution:
        """Begin or replay one executor attempt.

        Args:
            task_id: Owning Harness task.
            executor_kind: Executor Adapter kind.
            capability: Bounded executor capability.
            idempotency_key: Stable attempt key.
            request_digest: Digest of the redacted request.
            metadata: Non-sensitive attempt metadata.

        Returns:
            Existing or newly started execution.
        """
        return await self.store.start_execution(
            task_id=task_id,
            executor_kind=executor_kind,
            capability=capability,
            idempotency_key=idempotency_key,
            request_digest=request_digest,
            metadata=metadata,
        )

    async def settle(
        self,
        *,
        execution_id: str,
        idempotency_key: str,
        outcome: HarnessSettlementOutcome,
        result: dict,
        delivery: HarnessDeliveryReceipt,
        artifact: HarnessArtifactSpec | None = None,
    ) -> HarnessExecutionSettlement:
        """Record and apply one idempotent executor settlement.

        Args:
            execution_id: Attempt being closed.
            idempotency_key: Stable settlement callback key.
            outcome: Validated Harness task outcome.
            result: Redacted result summary and evidence.
            delivery: Minimal delivery evidence.
            artifact: Optional durable Artifact.

        Returns:
            Applied settlement receipt.

        Raises:
            ValueError: If delivery or Artifact invariants are violated.
        """
        if outcome not in {
            "completed",
            "review_required",
            "blocked",
            "failed",
            "cancelled",
        }:
            raise ValueError(f"unsupported settlement outcome: {outcome}")
        if delivery.mode not in {"confirmed", "runtime_owned", "not_required"}:
            raise ValueError(f"unsupported delivery mode: {delivery.mode}")
        if outcome == "completed" and delivery.mode not in {
            "confirmed",
            "not_required",
        }:
            raise ValueError("completed settlement requires confirmed delivery")
        execution = await self.store.get_execution(execution_id)
        if execution is None:
            raise LookupError(f"execution {execution_id!r} not found")
        if (
            outcome == "completed"
            and execution.executor_kind == "media"
            and artifact is None
        ):
            raise ValueError("media success requires an Artifact")
        if artifact is not None and (
            not artifact.context_id.strip()
            or not artifact.artifact_kind.strip()
            or not artifact.uri.strip()
            or not artifact.mime_type.strip()
        ):
            raise ValueError("Artifact identity fields must not be empty")

        request_payload = {
            "execution_id": execution_id,
            "outcome": outcome,
            "result": result,
            "delivery": asdict(delivery),
            "artifact": asdict(artifact) if artifact is not None else None,
        }
        request_digest = hashlib.sha256(
            json.dumps(
                request_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        receipt = await self.store.record_execution_settlement(
            execution_id=execution_id,
            outcome=outcome,
            idempotency_key=idempotency_key,
            request_digest=request_digest,
            result=result,
            delivery=asdict(delivery),
            artifact=artifact,
        )
        if receipt.status == "applied":
            return receipt
        return await self._apply(receipt)

    async def reconcile(self) -> int:
        """Apply durable pending settlements after callback or process failure.

        Returns:
            Number of settlements successfully applied.
        """
        applied = 0
        for receipt in await self.store.list_pending_execution_settlements():
            try:
                await self._apply(receipt)
                applied += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[executor_settlement] reconcile failed settlement=%s: %s",
                    receipt.settlement_id,
                    exc,
                )
        return applied

    async def _apply(
        self, receipt: HarnessExecutionSettlement
    ) -> HarnessExecutionSettlement:
        task = await self.store.get_task(receipt.task_id)
        if task is None:
            raise LookupError(f"task {receipt.task_id!r} not found")
        if task.status == receipt.outcome:
            return await self.store.mark_execution_settlement_applied(
                receipt.settlement_id
            )
        if task.status in HARNESS_TERMINAL_STATUSES:
            raise RuntimeError(
                f"settlement target {receipt.outcome!r} conflicts with terminal task {task.status!r}"
            )

        result = dict(receipt.result)
        if receipt.artifact_id:
            result["artifact_id"] = receipt.artifact_id
        reason = str(
            result.get("reason")
            or result.get("error")
            or result.get("summary")
            or receipt.outcome
        )
        if receipt.outcome == "completed":
            await self.engine.complete_task(receipt.task_id, result=result)
        elif receipt.outcome == "review_required":
            await self.engine.mark_review_required(
                receipt.task_id,
                reviewer_note=reason,
                result=result,
            )
        elif receipt.outcome == "blocked":
            await self.engine.set_status(
                receipt.task_id,
                "blocked",
                result=result,
                event_payload={"blocking_reason": reason},
            )
        elif receipt.outcome == "failed":
            await self.engine.fail_task(receipt.task_id, reason=reason)
        else:
            await self.engine.cancel_task(receipt.task_id, reason=reason)
        return await self.store.mark_execution_settlement_applied(receipt.settlement_id)
