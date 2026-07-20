from __future__ import annotations

from pathlib import Path

from dc_engines.harness import HarnessMemoryRecord, HarnessMemoryStore
from dc_engines.memory_governance import (
    MemoryGovernanceStore,
    TaskOutcomeGovernedDistiller,
)
from dc_engines.memory_governance.exporter import export_task_outcome_candidates
from dc_engines.memory_governance.obsidian_codec import render_governance_note


def _record() -> HarnessMemoryRecord:
    return HarnessMemoryRecord(
        memory_id="raw-memory-1",
        session_id="session-1",
        conversation_id="conversation-1",
        task_id="task-1",
        domain="project_followup",
        memory_kind="task_outcome",
        title="供应链风险跟进",
        summary="供应链风险已确认，建议提前备货 30 天。",
        payload={"result": {"complete_answer": "must not be distilled"}},
        created_at="2026-07-13T02:30:00Z",
    )


async def test_task_outcome_distillation_is_idempotent(tmp_path: Path) -> None:
    store = MemoryGovernanceStore(tmp_path / "governed_memory.db")
    distiller = TaskOutcomeGovernedDistiller(
        store=store,
        vault_path=tmp_path / "ObsidianVault",
    )

    first = await distiller.distill(_record())
    replay = await distiller.distill(_record())

    assert first.status == "created"
    assert replay.status == "unchanged"
    assert replay.memory_id == first.memory_id
    assert Path(first.note_path).is_file()
    assert len(store.list_memories()) == 1
    audits = store.list_audit(first.memory_id)
    assert [item["action"] for item in audits] == ["distillation_candidate_created"]


async def test_reviewed_candidate_is_not_overwritten(tmp_path: Path) -> None:
    store = MemoryGovernanceStore(tmp_path / "governed_memory.db")
    distiller = TaskOutcomeGovernedDistiller(
        store=store,
        vault_path=tmp_path / "ObsidianVault",
    )
    first = await distiller.distill(_record())
    memory = store.get_memory(first.memory_id)
    assert memory is not None
    memory.review_status = "approved"
    memory.approved_by = "reviewer-1"
    memory.approved_at = "2026-07-13T02:31:00Z"
    store.upsert_memory(memory)
    note_path = Path(first.note_path)
    note_path.write_text(render_governance_note(memory), encoding="utf-8")
    approved_markdown = note_path.read_text(encoding="utf-8")

    replay = await distiller.distill(_record())

    assert replay.status == "unchanged"
    assert store.get_memory(first.memory_id).review_status == "approved"
    assert note_path.read_text(encoding="utf-8") == approved_markdown
    assert len(store.list_audit(first.memory_id)) == 1


async def test_periodic_export_is_reconciliation_only(tmp_path: Path) -> None:
    memory_store = HarnessMemoryStore(tmp_path / "harness_memory.db")
    await memory_store.initialize()
    await memory_store.create_memory(
        session_id="session-1",
        conversation_id="conversation-1",
        task_id="task-1",
        domain="project_followup",
        memory_kind="task_outcome",
        title="供应链风险跟进",
        summary="供应链风险已确认，建议提前备货 30 天。",
        payload={"result": {"complete_answer": "must not be distilled"}},
    )
    store = MemoryGovernanceStore(tmp_path / "governed_memory.db")
    distiller = TaskOutcomeGovernedDistiller(
        store=store,
        vault_path=tmp_path / "ObsidianVault",
    )
    immediate = await distiller.distill(_record())
    audit_count = len(store.list_audit(immediate.memory_id))

    result = export_task_outcome_candidates(
        harness_memory_db_path=tmp_path / "harness_memory.db",
        vault_path=tmp_path / "ObsidianVault",
        store=store,
        limit=20,
        now="2026-07-13T02:32:00Z",
    )

    assert result.exported_count == 0
    assert result.skipped_count == 1
    assert result.memory_ids == []
    assert len(store.list_audit(immediate.memory_id)) == audit_count
