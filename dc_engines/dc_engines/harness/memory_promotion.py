from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from .contracts import HarnessTask
from .memory_store import HarnessMemoryRecord, HarnessMemoryStore

HarnessMemoryDistillationStatus = Literal["created", "recovered", "unchanged"]


@dataclass(slots=True, frozen=True)
class HarnessMemoryDistillationReceipt:
    """Result returned by a governed-memory distillation port.

    Attributes:
        memory_id: Stable governed-memory candidate identifier.
        status: Whether the candidate changed or was already current.
        note_path: Human-review adapter reference when available.
    """

    memory_id: str
    status: HarnessMemoryDistillationStatus
    note_path: str


class HarnessMemoryDistiller(Protocol):
    """Port used by Harness after a task outcome memory is promoted."""

    async def distill(
        self,
        record: HarnessMemoryRecord,
    ) -> HarnessMemoryDistillationReceipt:
        """Distill one bounded task outcome into governed memory.

        Args:
            record: Bounded Harness task-outcome memory.

        Returns:
            Durable governed-memory distillation receipt.
        """
        ...


@dataclass(slots=True)
class HarnessMemoryPromoter:
    store: HarnessMemoryStore

    async def promote_from_task(self, task: HarnessTask) -> HarnessMemoryRecord | None:
        summary = self._build_summary(task)
        if not summary:
            return None
        return await self.store.create_memory(
            session_id=task.session_id,
            conversation_id=task.conversation_id,
            task_id=task.task_id,
            domain=task.domain,
            memory_kind="task_outcome",
            title=task.title,
            summary=summary,
            payload={
                "result": task.result,
                "workflow_kind": task.payload.get("workflow_kind"),
            },
        )

    def _build_summary(self, task: HarnessTask) -> str:
        summary = str(task.result.get("summary", "")).strip()
        if summary:
            return summary[:200]

        workflow_validation = task.result.get("workflow_validation", {})
        if isinstance(workflow_validation, dict):
            workflow_kind = task.payload.get("workflow_kind", "workflow")
            missing_outputs = workflow_validation.get("missing_outputs", [])
            if missing_outputs:
                return (
                    f"{workflow_kind} 结果待补充，缺少字段: "
                    + ", ".join(str(item) for item in missing_outputs)
                )[:200]

        for key in ("strategy", "progress", "decision", "deliverables"):
            value = task.result.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:200]
        return ""
