from __future__ import annotations

from pathlib import Path

import pytest
from dc_engines.harness import (
    HarnessEngine,
    HarnessMemoryPromoter,
    HarnessMemoryStore,
    HarnessTaskCreateRequest,
    HarnessTaskStore,
)
from dc_engines.memory_governance import (
    MemoryGovernanceStore,
    TaskOutcomeGovernedDistiller,
)


async def _engine(tmp_path: Path, *, distiller=None) -> HarnessEngine:
    task_store = HarnessTaskStore(tmp_path / "harness.db")
    await task_store.initialize()
    memory_store = HarnessMemoryStore(tmp_path / "harness_memory.db")
    await memory_store.initialize()
    return HarnessEngine(
        task_store,
        memory_promoter=HarnessMemoryPromoter(memory_store),
        memory_distiller=distiller,
    )


@pytest.mark.asyncio
async def test_completed_task_immediately_creates_governed_candidate(
    tmp_path: Path,
) -> None:
    governance_store = MemoryGovernanceStore(tmp_path / "governed_memory.db")
    distiller = TaskOutcomeGovernedDistiller(
        store=governance_store,
        vault_path=tmp_path / "ObsidianVault",
    )
    engine = await _engine(tmp_path, distiller=distiller)
    task = await engine.create_task(
        HarnessTaskCreateRequest(
            title="供应链风险跟进",
            conversation_id="conversation-1",
            platform_id="lark",
            session_id="session-1",
            domain="project_followup",
            payload={},
        )
    )

    completed = await engine.complete_task(
        task.task_id,
        result={"summary": "供应链风险已确认，建议提前备货 30 天。"},
    )

    candidates = governance_store.list_memories(status="need_review")
    assert completed.status == "completed"
    assert len(candidates) == 1
    assert Path(candidates[0].obsidian_note_path).is_file()
    events = await engine.store.list_events(task.task_id)
    assert any(
        event.event_type == "governed_distillation_candidate_created"
        for event in events
    )


@pytest.mark.asyncio
async def test_distillation_failure_does_not_roll_back_completed_task(
    tmp_path: Path,
) -> None:
    class _FailingDistiller:
        async def distill(self, _record):
            raise OSError("vault unavailable")

    engine = await _engine(tmp_path, distiller=_FailingDistiller())
    task = await engine.create_task(
        HarnessTaskCreateRequest(
            title="供应链风险跟进",
            conversation_id="conversation-1",
            platform_id="lark",
            session_id="session-1",
            domain="project_followup",
            payload={},
        )
    )

    completed = await engine.complete_task(
        task.task_id,
        result={"summary": "供应链风险已确认。"},
    )

    assert completed.status == "completed"
    events = await engine.store.list_events(task.task_id)
    assert any(event.event_type == "governed_distillation_failed" for event in events)
