from __future__ import annotations

import sqlite3
from pathlib import Path

from dc_engines.memory_governance.exporter import export_memory_candidates
from dc_engines.memory_governance.importer import import_governance_notes
from dc_engines.memory_governance.obsidian_codec import render_governance_note
from dc_engines.memory_governance.store import MemoryGovernanceStore


def create_nas_memory_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE documents (
                doc_key TEXT PRIMARY KEY,
                rel_path TEXT NOT NULL,
                source_path TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                parser TEXT NOT NULL,
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                tags_json TEXT NOT NULL,
                indexed_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                project_id TEXT DEFAULT '',
                project_name TEXT DEFAULT '',
                doc_type TEXT DEFAULT '',
                owner TEXT DEFAULT '',
                confidence REAL DEFAULT 0,
                review_status TEXT DEFAULT 'need_review'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO documents (
                doc_key, rel_path, source_path, sha256, parser, title, summary,
                tags_json, indexed_at, metadata_json, project_id, project_name,
                doc_type, owner, confidence, review_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "doc_1",
                "projects/customer-a/delivery.md",
                "/Users/dianchi/nas_kb/projects/customer-a/delivery.md",
                "abc123",
                "md",
                "Customer A delivery",
                "Customer A delivery takes four weeks.",
                '["customer-a", "delivery"]',
                "2026-06-04T00:00:00Z",
                "{}",
                "customer-a",
                "Customer A",
                "执行方案",
                "谭媛尹",
                0.78,
                "need_review",
            ),
        )


def test_export_writes_review_note_with_required_frontmatter(tmp_path: Path) -> None:
    nas_db = tmp_path / "nas_memory.db"
    vault = tmp_path / "ObsidianVault"
    store = MemoryGovernanceStore(tmp_path / "governed_memory.db")
    store.initialize()
    create_nas_memory_db(nas_db)

    result = export_memory_candidates(
        nas_db_path=nas_db,
        vault_path=vault,
        store=store,
        limit=10,
        now="2026-06-04T00:01:00Z",
    )

    assert result.exported_count == 1
    assert result.skipped_count == 0
    note_path = result.note_paths[0]
    assert note_path.parent == vault / "40_MemoryGovernance" / "Inbox"
    markdown = note_path.read_text(encoding="utf-8")
    assert "memory_id: mem_nas_" in markdown
    assert "source_path: /Users/dianchi/nas_kb/projects/customer-a/delivery.md" in markdown
    assert "source_hash: sha256:abc123" in markdown
    assert "review_status: need_review" in markdown
    assert "sensitivity: internal" in markdown
    assert "governance_version: 1" in markdown
    assert "Customer A delivery takes four weeks." in markdown

    stored = store.list_memories(status="need_review")
    assert len(stored) == 1
    assert stored[0].obsidian_note_path == str(note_path)


def test_export_is_idempotent_for_same_source_document(tmp_path: Path) -> None:
    nas_db = tmp_path / "nas_memory.db"
    vault = tmp_path / "ObsidianVault"
    store = MemoryGovernanceStore(tmp_path / "governed_memory.db")
    store.initialize()
    create_nas_memory_db(nas_db)

    first = export_memory_candidates(
        nas_db_path=nas_db,
        vault_path=vault,
        store=store,
        limit=10,
        now="2026-06-04T00:01:00Z",
    )
    second = export_memory_candidates(
        nas_db_path=nas_db,
        vault_path=vault,
        store=store,
        limit=10,
        now="2026-06-04T00:02:00Z",
    )

    notes = list((vault / "40_MemoryGovernance" / "Inbox").glob("*.md"))
    assert first.note_paths == second.note_paths
    assert len(notes) == 1
    assert len(store.list_memories()) == 1


def test_export_does_not_overwrite_approved_memory(tmp_path: Path) -> None:
    nas_db = tmp_path / "nas_memory.db"
    vault = tmp_path / "ObsidianVault"
    store = MemoryGovernanceStore(tmp_path / "governed_memory.db")
    store.initialize()
    create_nas_memory_db(nas_db)

    first = export_memory_candidates(
        nas_db_path=nas_db,
        vault_path=vault,
        store=store,
        limit=10,
        now="2026-06-04T00:01:00Z",
    )
    memory = store.get_memory(first.memory_ids[0])
    assert memory is not None
    approved = memory
    approved.review_status = "approved"
    approved.approved_by = "dianchi"
    approved.approved_at = "2026-06-04T00:03:00Z"
    store.upsert_memory(approved)
    first.note_paths[0].write_text(
        render_governance_note(approved),
        encoding="utf-8",
    )

    second = export_memory_candidates(
        nas_db_path=nas_db,
        vault_path=vault,
        store=store,
        limit=10,
        now="2026-06-04T00:04:00Z",
    )

    assert second.exported_count == 0
    assert second.skipped_count == 1
    assert "review_status: approved" in first.note_paths[0].read_text(encoding="utf-8")


def test_import_is_idempotent_for_same_memory_id(tmp_path: Path) -> None:
    nas_db = tmp_path / "nas_memory.db"
    vault = tmp_path / "ObsidianVault"
    store = MemoryGovernanceStore(tmp_path / "governed_memory.db")
    store.initialize()
    create_nas_memory_db(nas_db)
    export_result = export_memory_candidates(
        nas_db_path=nas_db,
        vault_path=vault,
        store=store,
        limit=10,
        now="2026-06-04T00:01:00Z",
    )
    note_path = export_result.note_paths[0]
    markdown = note_path.read_text(encoding="utf-8")
    markdown = markdown.replace("review_status: need_review", "review_status: approved")
    markdown = markdown.replace("reviewer: ''", "reviewer: dianchi")
    markdown = markdown.replace(
        "Customer A delivery takes four weeks.",
        "Customer A delivery takes five weeks after finance approval.",
        1,
    )
    note_path.write_text(markdown, encoding="utf-8")

    first = import_governance_notes(
        vault_path=vault,
        store=store,
        now="2026-06-04T00:10:00Z",
        actor="obsidian-sync",
    )
    second = import_governance_notes(
        vault_path=vault,
        store=store,
        now="2026-06-04T00:11:00Z",
        actor="obsidian-sync",
    )

    memory_id = export_result.memory_ids[0]
    loaded = store.get_memory(memory_id)
    assert loaded is not None
    assert loaded.review_status == "approved"
    assert (
        loaded.canonical_text
        == "Customer A delivery takes five weeks after finance approval."
    )
    assert len(store.list_memories()) == 1
    assert first.imported_count == 1
    assert second.imported_count == 1
    assert len(store.list_decisions(memory_id)) == 1
    assert len(store.list_audit(memory_id)) == 2


def test_import_rejected_note_records_decision_but_remains_non_promoted(
    tmp_path: Path,
) -> None:
    nas_db = tmp_path / "nas_memory.db"
    vault = tmp_path / "ObsidianVault"
    store = MemoryGovernanceStore(tmp_path / "governed_memory.db")
    store.initialize()
    create_nas_memory_db(nas_db)
    export_result = export_memory_candidates(
        nas_db_path=nas_db,
        vault_path=vault,
        store=store,
        limit=10,
        now="2026-06-04T00:01:00Z",
    )
    note_path = export_result.note_paths[0]
    markdown = note_path.read_text(encoding="utf-8")
    markdown = markdown.replace("review_status: need_review", "review_status: rejected")
    markdown = markdown.replace("reviewer: ''", "reviewer: dianchi")
    note_path.write_text(markdown, encoding="utf-8")

    result = import_governance_notes(
        vault_path=vault,
        store=store,
        now="2026-06-04T00:10:00Z",
        actor="obsidian-sync",
    )

    memory_id = export_result.memory_ids[0]
    loaded = store.get_memory(memory_id)
    assert loaded is not None
    assert loaded.review_status == "rejected"
    assert result.imported_count == 1
    assert store.list_decisions(memory_id)[0]["decision"] == "rejected"
