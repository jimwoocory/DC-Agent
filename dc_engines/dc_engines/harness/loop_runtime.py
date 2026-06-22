from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal

from .contracts import HARNESS_TERMINAL_STATUSES, HarnessTask
from .task_store import HarnessTaskStore

LOOP_VERSION = "mvp-1"

LoopEventType = Literal[
    "loop_plan_created",
    "loop_action_started",
    "loop_observation_recorded",
    "loop_decision_recorded",
    "loop_settlement_decided",
    "loop_quality_gate_failed",
]
LoopStatus = Literal[
    "pending",
    "running",
    "ok",
    "blocked",
    "failed",
    "review_required",
]

LOOP_EVENT_TYPES: tuple[str, ...] = (
    "loop_plan_created",
    "loop_action_started",
    "loop_observation_recorded",
    "loop_decision_recorded",
    "loop_settlement_decided",
    "loop_quality_gate_failed",
)


@dataclass(slots=True)
class LoopStep:
    step_id: str
    kind: Literal["plan", "action", "observation", "decision", "settlement"]
    summary: str
    status: LoopStatus
    evidence: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return _json_safe(
            {
                "loop_version": LOOP_VERSION,
                "step_id": self.step_id,
                "kind": self.kind,
                "summary": self.summary,
                "status": self.status,
                "evidence": self.evidence,
                "metadata": self.metadata,
            }
        )


@dataclass(slots=True)
class LoopPlan:
    goal: str
    steps: list[LoopStep]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class LoopSettlement:
    status: Literal["completed", "blocked", "review_required", "failed"]
    summary: str
    evidence: list[dict[str, Any]] = field(default_factory=list)
    blocking_reason: str = ""
    review_reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_step(self) -> LoopStep:
        if self.status == "completed":
            step_status: LoopStatus = "ok"
        elif self.status == "review_required":
            step_status = "review_required"
        elif self.status == "blocked":
            step_status = "blocked"
        else:
            step_status = "failed"
        return LoopStep(
            step_id=f"settlement:{self.status}",
            kind="settlement",
            summary=self.summary,
            status=step_status,
            evidence=self.evidence,
            metadata={
                **self.metadata,
                "settlement_status": self.status,
                "blocking_reason": self.blocking_reason,
                "review_reason": self.review_reason,
            },
        )


class LoopEventRecorder:
    def __init__(self, store: HarnessTaskStore) -> None:
        self.store = store

    async def record(self, task_id: str, event_type: LoopEventType, step: LoopStep):
        task = await self.store.get_task(task_id)
        if task is None:
            raise LookupError(f"task {task_id!r} not found")
        if task.status in HARNESS_TERMINAL_STATUSES:
            raise RuntimeError(
                f"cannot append loop event to terminal task {task_id!r} ({task.status})"
            )
        return await self.store.append_event(task_id, event_type, step.to_payload())

    async def record_quality_gate_failed(
        self,
        task_id: str,
        *,
        reason: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        await self.record(
            task_id,
            "loop_quality_gate_failed",
            LoopStep(
                step_id="quality_gate:failed",
                kind="decision",
                summary=reason,
                status="failed",
                metadata=metadata or {},
            ),
        )


class LoopOrchestrator:
    def __init__(self, store: HarnessTaskStore) -> None:
        self.store = store
        self.recorder = LoopEventRecorder(store)

    async def create_plan(self, task: HarnessTask) -> LoopPlan:
        required_outputs = task.payload.get("required_outputs") or []
        steps = [
            LoopStep(
                step_id="plan:intake",
                kind="plan",
                summary="Confirm goal and available source context.",
                status="pending",
                metadata={"domain": task.domain},
            ),
            LoopStep(
                step_id="plan:execute",
                kind="plan",
                summary="Execute the task and record action/observation events.",
                status="pending",
                metadata={"required_outputs": required_outputs},
            ),
            LoopStep(
                step_id="plan:settle",
                kind="plan",
                summary="Settle the task as completed, blocked, or review_required with evidence.",
                status="pending",
                metadata={
                    "review_required_by_default": bool(
                        task.payload.get("review_required_by_default")
                    )
                },
            ),
        ]
        return LoopPlan(goal=task.title, steps=steps)

    async def ensure_plan_created(self, task: HarnessTask) -> None:
        events = await self.store.list_events(task.task_id)
        if any(event.event_type == "loop_plan_created" for event in events):
            return
        plan = await self.create_plan(task)
        await self.recorder.record(
            task.task_id,
            "loop_plan_created",
            LoopStep(
                step_id="plan:created",
                kind="plan",
                summary=plan.goal,
                status="pending",
                metadata={
                    "steps": [step.to_payload() for step in plan.steps],
                    **plan.metadata,
                },
            ),
        )

    async def record_action_started(
        self,
        task_id: str,
        *,
        step_id: str,
        summary: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        await self.recorder.record(
            task_id,
            "loop_action_started",
            LoopStep(
                step_id=step_id,
                kind="action",
                summary=summary,
                status="running",
                metadata=metadata or {},
            ),
        )

    async def record_observation(
        self,
        task_id: str,
        *,
        step_id: str,
        summary: str,
        evidence: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        await self.recorder.record(
            task_id,
            "loop_observation_recorded",
            LoopStep(
                step_id=step_id,
                kind="observation",
                summary=summary,
                status="ok",
                evidence=evidence or [],
                metadata=metadata or {},
            ),
        )

    async def record_decision(
        self,
        task_id: str,
        *,
        step_id: str,
        summary: str,
        status: LoopStatus = "ok",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        await self.recorder.record(
            task_id,
            "loop_decision_recorded",
            LoopStep(
                step_id=step_id,
                kind="decision",
                summary=summary,
                status=status,
                metadata=metadata or {},
            ),
        )

    async def settle_for_completion(
        self,
        task: HarnessTask,
        result: dict[str, Any] | None,
    ) -> LoopSettlement:
        result_payload = result or {}
        evidence = extract_evidence(result_payload)
        if not evidence:
            reason = "completed settlement requires evidence"
            await self.recorder.record_quality_gate_failed(
                task.task_id,
                reason=reason,
                metadata={"result_keys": sorted(result_payload.keys())},
            )
            raise RuntimeError(reason)
        settlement = LoopSettlement(
            status="completed",
            summary=str(result_payload.get("summary") or "Task completed."),
            evidence=evidence,
            metadata={
                "source": result_payload.get("source", ""),
                "quality": result_payload.get("quality", ""),
            },
        )
        await self.recorder.record(
            task.task_id,
            "loop_settlement_decided",
            settlement.to_step(),
        )
        return settlement

    async def settle_for_blocked(
        self,
        task: HarnessTask,
        *,
        blocking_reason: str,
        summary: str,
        metadata: dict[str, Any] | None = None,
    ) -> LoopSettlement:
        if not blocking_reason.strip():
            raise RuntimeError("blocked settlement requires blocking_reason")
        settlement = LoopSettlement(
            status="blocked",
            summary=summary,
            blocking_reason=blocking_reason,
            metadata=metadata or {},
        )
        await self.recorder.record(
            task.task_id,
            "loop_settlement_decided",
            settlement.to_step(),
        )
        return settlement

    async def settle_for_review_required(
        self,
        task: HarnessTask,
        *,
        review_reason: str,
        summary: str,
        result: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> LoopSettlement:
        if not review_reason.strip():
            raise RuntimeError("review_required settlement requires review_reason")
        settlement = LoopSettlement(
            status="review_required",
            summary=summary,
            review_reason=review_reason,
            evidence=extract_evidence(result or {}),
            metadata=metadata or {},
        )
        await self.recorder.record(
            task.task_id,
            "loop_settlement_decided",
            settlement.to_step(),
        )
        return settlement


def extract_evidence(result: dict[str, Any]) -> list[dict[str, Any]]:
    evidence = result.get("evidence")
    if isinstance(evidence, list):
        normalized = [
            item if isinstance(item, dict) else {"value": item}
            for item in evidence
            if _has_value(item)
        ]
        if normalized:
            return _json_safe(normalized)

    extracted: list[dict[str, Any]] = []
    for key in ("source_citations", "hits"):
        value = result.get(key)
        if isinstance(value, list) and value:
            extracted.append({"type": key, "items": value})
    for key in (
        "output_url",
        "output_path",
        "image_path",
        "file_path",
        "artifact_path",
        "source",
        "response_preview",
        "summary",
    ):
        value = result.get(key)
        if _has_value(value):
            extracted.append({"type": key, "value": value})
    return _json_safe(extracted)


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


def _json_safe(value: Any) -> Any:
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except TypeError:
        if isinstance(value, dict):
            return {str(key): _json_safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [_json_safe(item) for item in value]
        return str(value)
