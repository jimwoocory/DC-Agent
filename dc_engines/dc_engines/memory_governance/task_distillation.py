"""Event-driven Harness task-outcome distillation into governed memory."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from dc_engines.harness import (
    HarnessMemoryDistillationReceipt,
    HarnessMemoryRecord,
)

from .exporter import export_task_outcome_candidate
from .store import MemoryGovernanceStore


@dataclass(slots=True)
class TaskOutcomeGovernedDistiller:
    """Publish bounded Harness task outcomes to the governance review adapter.

    Attributes:
        store: Authoritative governed-memory state store.
        vault_path: Obsidian vault used as the human review adapter.
        actor: Audit actor for immediate event-driven publication.
    """

    store: MemoryGovernanceStore
    vault_path: Path
    actor: str = "harness-task-distillation"

    async def distill(
        self,
        record: HarnessMemoryRecord,
    ) -> HarnessMemoryDistillationReceipt:
        """Distill one bounded task summary without blocking the event loop.

        Args:
            record: Harness task-outcome memory containing a bounded summary.

        Returns:
            Stable candidate and Obsidian review-note receipt.
        """

        exported = await asyncio.to_thread(
            export_task_outcome_candidate,
            record=record,
            vault_path=self.vault_path,
            store=self.store,
            now=record.created_at,
            actor=self.actor,
        )
        return HarnessMemoryDistillationReceipt(
            memory_id=exported.memory_id,
            status=exported.status,
            note_path=str(exported.note_path),
        )
