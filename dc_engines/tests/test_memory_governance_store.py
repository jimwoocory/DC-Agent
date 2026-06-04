from __future__ import annotations

from pathlib import Path

import pytest
from dc_engines.memory_governance.models import GovernedMemory, ReviewDecision
from dc_engines.memory_governance.store import MemoryGovernanceStore


def sample_memory(
    *,
    memory_id: str = "mem_test_001",
    review_status: str = "need_review",
    sensitivity: str = "internal",
) -> GovernedMemory:
    return GovernedMemory(
        memory_id=memory_id,
        source_system="nas",
        source_id="nas:doc-1",
        source_path="/Users/dianchi/nas_kb/doc-1.md",
        source_hash="sha256:abc",
        title="Customer A delivery",
        summary="Customer A delivery takes four weeks.",
        canonical_text="Customer A delivery takes four weeks.",
        memory_kind="process",
        review_status=review_status,  # type: ignore[arg-type]
        confidence=0.7,
        sensitivity=sensitivity,  # type: ignore[arg-type]
        owner="",
        project_id="",
        tags=["customer-a", "delivery"],
        links=["[[Customer A delivery]]"],
        obsidian_note_path="",
        governance_version=1,
        created_at="2026-06-04T00:00:00Z",
        updated_at="2026-06-04T00:00:00Z",
        approved_at="",
        approved_by="",
    )


def test_store_upserts_memory_and_reads_it_back(tmp_path: Path) -> None:
    store = MemoryGovernanceStore(tmp_path / "governed_memory.db")
    store.initialize()

    store.upsert_memory(sample_memory())
    loaded = store.get_memory("mem_test_001")

    assert loaded is not None
    assert loaded.memory_id == "mem_test_001"
    assert loaded.review_status == "need_review"
    assert loaded.source_path == "/Users/dianchi/nas_kb/doc-1.md"
    assert loaded.tags == ["customer-a", "delivery"]
    assert loaded.links == ["[[Customer A delivery]]"]


def test_upsert_updates_existing_memory_without_duplicate(tmp_path: Path) -> None:
    store = MemoryGovernanceStore(tmp_path / "governed_memory.db")
    store.initialize()
    store.upsert_memory(sample_memory())
    store.upsert_memory(
        sample_memory(
            review_status="approved",
        )
    )

    loaded = store.get_memory("mem_test_001")

    assert loaded is not None
    assert loaded.review_status == "approved"
    assert [memory.memory_id for memory in store.list_memories()] == ["mem_test_001"]


def test_list_memories_filters_by_status(tmp_path: Path) -> None:
    store = MemoryGovernanceStore(tmp_path / "governed_memory.db")
    store.initialize()
    store.upsert_memory(sample_memory(memory_id="mem_1", review_status="approved"))
    store.upsert_memory(sample_memory(memory_id="mem_2", review_status="need_review"))

    approved = store.list_memories(status="approved")

    assert [memory.memory_id for memory in approved] == ["mem_1"]


def test_decision_and_audit_records_are_append_only(tmp_path: Path) -> None:
    store = MemoryGovernanceStore(tmp_path / "governed_memory.db")
    store.initialize()
    store.upsert_memory(sample_memory())

    decision = ReviewDecision(
        decision_id="dec_001",
        memory_id="mem_test_001",
        decision="approved",
        reviewer="dianchi",
        reason="Verified source document.",
        before={"review_status": "need_review"},
        after={"review_status": "approved"},
        created_at="2026-06-04T00:01:00Z",
    )
    store.record_decision(decision)
    audit_id = store.append_audit(
        "mem_test_001",
        "import_decision",
        "dianchi",
        {"decision": "approved"},
        created_at="2026-06-04T00:02:00Z",
    )

    decisions = store.list_decisions("mem_test_001")
    audits = store.list_audit("mem_test_001")

    assert len(decisions) == 1
    assert decisions[0]["before"] == {"review_status": "need_review"}
    assert decisions[0]["after"] == {"review_status": "approved"}
    assert len(audits) == 1
    assert audits[0]["audit_id"] == audit_id
    assert audits[0]["payload"] == {"decision": "approved"}


def test_governed_memory_rejects_invalid_status() -> None:
    with pytest.raises(ValueError, match="review_status"):
        sample_memory(review_status="unknown")


def test_governed_memory_rejects_secret_typo() -> None:
    with pytest.raises(ValueError, match="sensitivity"):
        sample_memory(sensitivity="private")
