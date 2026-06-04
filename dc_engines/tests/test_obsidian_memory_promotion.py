from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from dc_engines.memory_governance.models import GovernedMemory
from dc_engines.memory_governance.promoter import promote_governed_memories
from dc_engines.memory_governance.recall import list_recall_memories
from dc_engines.memory_governance.store import MemoryGovernanceStore


def memory(
    memory_id: str,
    *,
    status: str = "approved",
    sensitivity: str = "internal",
    source_id: str = "nas:doc_1",
    title: str = "Customer A delivery",
) -> GovernedMemory:
    return GovernedMemory(
        memory_id=memory_id,
        source_system="nas",
        source_id=source_id,
        source_path=f"/Users/dianchi/nas_kb/{memory_id}.md",
        source_hash=f"sha256:{memory_id}",
        title=title,
        summary=f"{title} takes four weeks.",
        canonical_text=f"{title} takes four weeks after approval.",
        memory_kind="process",
        review_status=status,  # type: ignore[arg-type]
        confidence=0.92,
        sensitivity=sensitivity,  # type: ignore[arg-type]
        owner="谭媛尹",
        project_id="customer-a",
        tags=["dc-agent-memory", "delivery"],
        links=[f"[[{title}]]"],
        obsidian_note_path="",
        governance_version=1,
        created_at="2026-06-04T00:00:00Z",
        updated_at="2026-06-04T00:00:00Z",
        approved_at="2026-06-04T00:10:00Z" if status == "approved" else "",
        approved_by="dianchi" if status == "approved" else "",
    )


def create_nas_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE documents (
                doc_key TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                review_status TEXT DEFAULT 'need_review',
                owner TEXT DEFAULT '',
                project_id TEXT DEFAULT '',
                confidence REAL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            INSERT INTO documents (
                doc_key, title, review_status, owner, project_id, confidence
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("doc_1", "Customer A delivery", "need_review", "", "", 0.5),
        )


def test_promoter_only_promotes_approved_internal_memory(tmp_path: Path) -> None:
    store = MemoryGovernanceStore(tmp_path / "governed_memory.db")
    store.initialize()
    nas_db = tmp_path / "nas_memory.db"
    overrides = tmp_path / "nas_memory_overrides.json"
    create_nas_db(nas_db)
    store.upsert_memory(memory("mem_approved"))
    store.upsert_memory(memory("mem_rejected", status="rejected", source_id="nas:doc_2"))
    store.upsert_memory(
        memory(
            "mem_sensitive",
            status="sensitive_blocked",
            source_id="nas:doc_3",
        )
    )
    store.upsert_memory(memory("mem_secret", sensitivity="secret", source_id="nas:doc_4"))

    result = promote_governed_memories(
        store=store,
        nas_db_path=nas_db,
        overrides_path=overrides,
        now="2026-06-04T00:20:00Z",
        actor="promoter-test",
    )

    assert result.promoted_memory_ids == ["mem_approved"]
    assert sorted(result.skipped_memory_ids) == [
        "mem_rejected",
        "mem_secret",
        "mem_sensitive",
    ]
    with sqlite3.connect(nas_db) as conn:
        row = conn.execute(
            "SELECT review_status, owner, project_id, confidence FROM documents WHERE doc_key = 'doc_1'"
        ).fetchone()
    assert row == ("confirmed", "谭媛尹", "customer-a", 0.92)
    overrides_data = json.loads(overrides.read_text(encoding="utf-8"))
    assert overrides_data["projects"]["Customer A delivery"]["review_status"] == "confirmed"
    assert len(store.list_audit("mem_approved")) == 1
    assert store.list_audit("mem_approved")[0]["action"] == "promoted_to_recall"


def test_promoter_dry_run_does_not_write_outputs(tmp_path: Path) -> None:
    store = MemoryGovernanceStore(tmp_path / "governed_memory.db")
    store.initialize()
    nas_db = tmp_path / "nas_memory.db"
    overrides = tmp_path / "nas_memory_overrides.json"
    create_nas_db(nas_db)
    store.upsert_memory(memory("mem_approved"))

    result = promote_governed_memories(
        store=store,
        nas_db_path=nas_db,
        overrides_path=overrides,
        now="2026-06-04T00:20:00Z",
        actor="promoter-test",
        dry_run=True,
    )

    assert result.promoted_memory_ids == ["mem_approved"]
    assert not overrides.exists()
    with sqlite3.connect(nas_db) as conn:
        status = conn.execute(
            "SELECT review_status FROM documents WHERE doc_key = 'doc_1'"
        ).fetchone()[0]
    assert status == "need_review"
    assert store.list_audit("mem_approved") == []


def test_recall_filters_to_approved_by_default(tmp_path: Path) -> None:
    store = MemoryGovernanceStore(tmp_path / "governed_memory.db")
    store.initialize()
    store.upsert_memory(memory("mem_approved", title="Launch SOP"))
    store.upsert_memory(memory("mem_need_review", status="need_review", title="Launch draft"))
    store.upsert_memory(memory("mem_rejected", status="rejected", title="Launch rejected"))
    store.upsert_memory(memory("mem_secret", sensitivity="secret", title="Launch secret"))
    store.upsert_memory(
        memory(
            "mem_sensitive_blocked",
            status="sensitive_blocked",
            title="Launch sensitive",
        )
    )

    memories = list_recall_memories(store=store, query="Launch")

    assert [item.memory_id for item in memories] == ["mem_approved"]


def test_recall_admin_path_can_include_non_default_memory(tmp_path: Path) -> None:
    store = MemoryGovernanceStore(tmp_path / "governed_memory.db")
    store.initialize()
    store.upsert_memory(memory("mem_approved", title="Budget SOP"))
    store.upsert_memory(memory("mem_secret", sensitivity="secret", title="Budget secret"))
    store.upsert_memory(memory("mem_need_review", status="need_review", title="Budget draft"))

    memories = list_recall_memories(
        store=store,
        query="Budget",
        include_unreviewed=True,
        include_sensitive=True,
    )

    assert [item.memory_id for item in memories] == [
        "mem_approved",
        "mem_need_review",
        "mem_secret",
    ]
