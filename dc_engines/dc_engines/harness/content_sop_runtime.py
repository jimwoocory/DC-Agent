from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from .contracts import HarnessTask
from .engine import HarnessEngine
from .workflows import validate_workflow_result

ContentSopDispatchAction = Literal["send_material_intake", "dispatch_hermes"]


@dataclass(frozen=True, slots=True)
class ContentSopDispatchDecision:
    action: ContentSopDispatchAction
    reason: str
    hermes_payload: dict[str, Any] | None = None

    @property
    def should_dispatch(self) -> bool:
        return self.action == "dispatch_hermes"


def plan_content_sop_dispatch(task: HarnessTask) -> ContentSopDispatchDecision:
    """Decide the next runtime step for a content SOP Harness task."""
    payload = task.payload or {}
    if payload.get("workflow_kind") != "content_sop_workflow":
        raise ValueError("task is not a content_sop_workflow")

    if (
        payload.get("generation_allowed") is False
        or payload.get("lifecycle_stage") == "needs_materials"
    ):
        return ContentSopDispatchDecision(
            action="send_material_intake",
            reason="required materials are missing; generation must stay paused",
        )

    return ContentSopDispatchDecision(
        action="dispatch_hermes",
        reason="materials ready; dispatch structured brief to Hermes",
        hermes_payload={
            "task_id": task.task_id,
            "workflow_kind": "content_sop_workflow",
            "brief": payload.get("brief", ""),
            "department_id": payload.get("department_id", ""),
            "scenario_id": payload.get("scenario_id", ""),
            "content_type": payload.get("content_type", ""),
            "knowledge_context": payload.get("knowledge_context", ""),
            "source_citations": payload.get("source_citations", []),
            "expected_outputs": payload.get("expected_outputs", []),
            "creative_assumptions": payload.get("creative_assumptions", []),
            "review_required_by_default": payload.get(
                "review_required_by_default", True
            ),
        },
    )


async def settle_content_sop_result(
    engine: HarnessEngine,
    task: HarnessTask,
    result: dict[str, Any],
) -> HarnessTask:
    """Validate a generated content SOP result and move the task lifecycle."""
    payload = task.payload or {}
    validation = validate_workflow_result(payload, result)
    if validation is None:
        return await engine.fail_task(
            task.task_id,
            reason="content_sop_workflow validation unavailable",
        )
    if not validation.is_valid:
        return await engine.fail_task(
            task.task_id,
            reason="; ".join(validation.missing_outputs),
        )
    return await engine.mark_review_required(
        task.task_id,
        reviewer_note="内容 SOP 交付物已生成，等待员工确认后外发。",
        result={
            **result,
            "lifecycle_stage": "review_required",
            "quality_status": "review_required",
        },
    )
