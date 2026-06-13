"""Staleness 扫描：NAS 源内容变更的 approved 记忆必须打回 Inbox 重审。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from dc_engines.memory_governance.models import GovernedMemory
from dc_engines.memory_governance.staleness import mark_stale_memories
from dc_engines.memory_governance.store import MemoryGovernanceStore

NOW = "2026-06-13T08:00:00Z"


def _nas_db(path: Path, rows: list[tuple[str, str]]) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE documents (doc_key TEXT PRIMARY KEY, sha256 TEXT)")
        conn.executemany("INSERT INTO documents VALUES (?, ?)", rows)


def _approved_nas_memory(memory_id: str, doc_key: str, source_hash: str) -> GovernedMemory:
    return GovernedMemory(
        memory_id=memory_id,
        source_system="nas",
        source_id=f"nas:{doc_key}",
        source_path=f"/nas/{doc_key}.md",
        source_hash=source_hash,
        title=f"doc {doc_key}",
        summary="summary",
        canonical_text="summary",
        memory_kind="document",
        review_status="approved",
        confidence=0.8,
        sensitivity="internal",
        created_at=NOW,
        updated_at=NOW,
    )


def test_changed_source_hash_marks_memory_stale_and_rewrites_note(tmp_path: Path):
    nas_db = tmp_path / "nas.db"
    _nas_db(nas_db, [("doc_1", "newhash")])
    store = MemoryGovernanceStore(tmp_path / "governed.db")
    store.initialize()
    store.upsert_memory(_approved_nas_memory("mem_1", "doc_1", "sha256:oldhash"))

    result = mark_stale_memories(
        store=store, nas_db_path=nas_db, vault_path=tmp_path / "vault", now=NOW
    )

    assert result.scanned_count == 1
    assert result.stale_count == 1
    assert result.stale_memory_ids == ["mem_1"]
    updated = store.get_memory("mem_1")
    assert updated is not None
    assert updated.review_status == "stale"
    note = Path(updated.obsidian_note_path)
    assert note.exists()
    assert "review_status: stale" in note.read_text(encoding="utf-8")
    audit = store.list_audit("mem_1")
    assert any(entry["action"] == "marked_stale" for entry in audit)


def test_unchanged_source_hash_is_untouched(tmp_path: Path):
    nas_db = tmp_path / "nas.db"
    _nas_db(nas_db, [("doc_1", "samehash")])
    store = MemoryGovernanceStore(tmp_path / "governed.db")
    store.initialize()
    store.upsert_memory(_approved_nas_memory("mem_1", "doc_1", "sha256:samehash"))

    result = mark_stale_memories(
        store=store, nas_db_path=nas_db, vault_path=tmp_path / "vault", now=NOW
    )

    assert result.stale_count == 0
    memory = store.get_memory("mem_1")
    assert memory is not None
    assert memory.review_status == "approved"


def test_missing_source_is_reported_not_marked(tmp_path: Path):
    nas_db = tmp_path / "nas.db"
    _nas_db(nas_db, [])
    store = MemoryGovernanceStore(tmp_path / "governed.db")
    store.initialize()
    store.upsert_memory(_approved_nas_memory("mem_1", "doc_gone", "sha256:aaa"))

    result = mark_stale_memories(
        store=store, nas_db_path=nas_db, vault_path=tmp_path / "vault", now=NOW
    )

    assert result.stale_count == 0
    assert result.missing_source_count == 1
    assert result.missing_source_memory_ids == ["mem_1"]
    memory = store.get_memory("mem_1")
    assert memory is not None
    assert memory.review_status == "approved"


def test_non_nas_sources_are_skipped(tmp_path: Path):
    nas_db = tmp_path / "nas.db"
    _nas_db(nas_db, [])
    store = MemoryGovernanceStore(tmp_path / "governed.db")
    store.initialize()
    memory = _approved_nas_memory("mem_1", "x", "sha256:aaa")
    memory.source_system = "harness"
    memory.source_id = "harness:task:t1"
    store.upsert_memory(memory)

    result = mark_stale_memories(
        store=store, nas_db_path=nas_db, vault_path=tmp_path / "vault", now=NOW
    )

    assert result.scanned_count == 0
    assert result.stale_count == 0
