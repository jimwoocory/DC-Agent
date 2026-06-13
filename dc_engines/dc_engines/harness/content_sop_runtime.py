from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from dc_engines.memory_governance.exporter import export_content_sop_memory_candidate
from dc_engines.memory_governance.store import MemoryGovernanceStore
from dc_engines.spiral_evolution import build_spiral_evolution_snapshot

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
            "spiral_evolution": payload.get("spiral_evolution", {}),
            "review_required_by_default": payload.get(
                "review_required_by_default", True
            ),
        },
    )


async def settle_content_sop_result(
    engine: HarnessEngine,
    task: HarnessTask,
    result: dict[str, Any],
    *,
    memory_governance_store: MemoryGovernanceStore | None = None,
    obsidian_vault_path: Path | str | None = None,
    now: str | None = None,
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
    spiral = build_spiral_evolution_snapshot(task, result)
    settled = await engine.mark_review_required(
        task.task_id,
        reviewer_note="内容 SOP 交付物已生成，等待员工确认后外发。",
        result={
            **result,
            "lifecycle_stage": "review_required",
            "quality_status": "review_required",
            "spiral_evolution": spiral,
            "memory_governance_export": {"status": "not_configured"},
        },
    )
    if memory_governance_store is None or obsidian_vault_path is None:
        return settled
    memory_export = _export_spiral_memory_candidate(
        spiral,
        memory_governance_store=memory_governance_store,
        obsidian_vault_path=obsidian_vault_path,
        now=now or _utcnow(),
    )
    try:
        return await engine.set_status(
            task.task_id,
            "review_required",
            result={**settled.result, "memory_governance_export": memory_export},
        )
    except Exception:  # noqa: BLE001
        return settled


def _export_spiral_memory_candidate(
    spiral: dict[str, Any],
    *,
    memory_governance_store: MemoryGovernanceStore | None,
    obsidian_vault_path: Path | str | None,
    now: str,
) -> dict[str, Any]:
    if memory_governance_store is None or obsidian_vault_path is None:
        return {"status": "not_configured"}
    try:
        result = export_content_sop_memory_candidate(
            candidate=spiral.get("memory_candidate") or {},
            vault_path=obsidian_vault_path,
            store=memory_governance_store,
            now=now,
        )
    except Exception as exc:  # noqa: BLE001
        return {"status": "failed", "error": str(exc)[:300]}
    return {
        "status": "exported" if result.exported_count else "skipped",
        "exported_count": result.exported_count,
        "skipped_count": result.skipped_count,
        "memory_ids": result.memory_ids,
        "note_paths": [str(path) for path in result.note_paths],
    }


def _utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
