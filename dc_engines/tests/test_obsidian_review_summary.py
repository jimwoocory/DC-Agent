from __future__ import annotations

from pathlib import Path

from dc_engines.memory_governance.models import GovernedMemory
from dc_engines.memory_governance.review_summary import build_review_summary
from dc_engines.memory_governance.store import MemoryGovernanceStore


def memory(
    memory_id: str,
    *,
    status: str = "need_review",
    source_system: str = "nas",
    owner: str = "",
    title: str = "Customer A delivery",
) -> GovernedMemory:
    return GovernedMemory(
        memory_id=memory_id,
        source_system=source_system,  # type: ignore[arg-type]
        source_id=f"{source_system}:{memory_id}",
        source_path=f"/tmp/{memory_id}.md",
        source_hash=f"sha256:{memory_id}",
        title=title,
        summary=f"{title} summary",
        canonical_text=f"{title} canonical text",
        memory_kind="fact",
        review_status=status,  # type: ignore[arg-type]
        confidence=0.7,
        sensitivity="internal",
        owner=owner,
        project_id="",
        tags=["dc-agent-memory"],
        links=[],
        obsidian_note_path=f"/vault/40_MemoryGovernance/Inbox/{memory_id}.md",
        governance_version=1,
        created_at="2026-06-04T00:00:00Z",
        updated_at=f"2026-06-04T00:0{memory_id[-1]}:00Z",
        approved_at="",
        approved_by="",
    )


def test_review_summary_counts_need_review_items_for_daily_card(
    tmp_path: Path,
) -> None:
    store = MemoryGovernanceStore(tmp_path / "governed_memory.db")
    store.initialize()
    store.upsert_memory(memory("mem_1", owner="content", title="Content SOP candidate"))
    store.upsert_memory(
        memory("mem_2", source_system="harness", owner="ops", title="Task outcome")
    )
    store.upsert_memory(memory("mem_3", status="approved", owner="content"))

    summary = build_review_summary(store=store, limit=1)

    assert summary.total_need_review == 2
    assert summary.by_owner == {"content": 1, "ops": 1}
    assert summary.by_source_system == {"harness": 1, "nas": 1}
    assert len(summary.items) == 1
    assert summary.items[0]["memory_id"] == "mem_2"


def test_review_summary_renders_markdown_without_sending_notifications(
    tmp_path: Path,
) -> None:
    store = MemoryGovernanceStore(tmp_path / "governed_memory.db")
    store.initialize()
    store.upsert_memory(memory("mem_1", owner="content", title="Content SOP candidate"))

    summary = build_review_summary(store=store, limit=5)

    markdown = summary.to_markdown()
    assert "Obsidian Memory Review" in markdown
    assert "待审 1" in markdown
    assert "Content SOP candidate" in markdown
