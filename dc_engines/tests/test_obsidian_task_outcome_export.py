"""harness task_outcome 记忆 → Obsidian 治理 Inbox 导出。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from dc_engines.memory_governance.exporter import (
    export_task_outcome_candidates,
    stable_task_outcome_memory_id,
)
from dc_engines.memory_governance.store import MemoryGovernanceStore

NOW = "2026-06-13T08:00:00Z"


def _harness_memory_db(path: Path, rows: list[dict]) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE harness_memories (
                memory_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                domain TEXT NOT NULL,
                memory_kind TEXT NOT NULL,
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        for row in rows:
            conn.execute(
                """
                INSERT INTO harness_memories VALUES
                (:memory_id, :session_id, :conversation_id, :task_id, :domain,
                 :memory_kind, :title, :summary, :payload_json, :created_at)
                """,
                row,
            )


def _task_outcome_row(task_id: str, **overrides) -> dict:
    row = {
        "memory_id": f"hm_{task_id}",
        "session_id": "lark:tenant:user",
        "conversation_id": "conv-1",
        "task_id": task_id,
        "domain": "project",
        "memory_kind": "task_outcome",
        "title": "项目跟进 | 供应链延期风险",
        "summary": "供应链延期风险：建议提前备货 30 天。",
        "payload_json": "{}",
        "created_at": NOW,
    }
    row.update(overrides)
    return row


def test_export_writes_need_review_note_for_task_outcome(tmp_path: Path):
    db = tmp_path / "harness_memory.db"
    _harness_memory_db(db, [_task_outcome_row("task_1")])
    store = MemoryGovernanceStore(tmp_path / "governed.db")

    result = export_task_outcome_candidates(
        harness_memory_db_path=db,
        vault_path=tmp_path / "vault",
        store=store,
        now=NOW,
    )

    assert result.exported_count == 1
    memory_id = stable_task_outcome_memory_id("task_1")
    assert result.memory_ids == [memory_id]
    memory = store.get_memory(memory_id)
    assert memory is not None
    assert memory.review_status == "need_review"
    assert memory.source_system == "harness"
    assert memory.source_id == "harness:task:task_1"
    assert memory.owner == "project"
    assert "harness-task" in memory.tags
    note = result.note_paths[0]
    assert note.exists()
    content = note.read_text(encoding="utf-8")
    assert "review_status: need_review" in content
    assert "供应链延期风险" in content


def test_export_is_idempotent_and_preserves_review_decisions(tmp_path: Path):
    db = tmp_path / "harness_memory.db"
    _harness_memory_db(db, [_task_outcome_row("task_1")])
    store = MemoryGovernanceStore(tmp_path / "governed.db")

    first = export_task_outcome_candidates(
        harness_memory_db_path=db, vault_path=tmp_path / "vault", store=store, now=NOW
    )
    assert first.exported_count == 1

    # 人审通过后再导出：不得覆盖审核结果
    memory_id = stable_task_outcome_memory_id("task_1")
    memory = store.get_memory(memory_id)
    assert memory is not None
    memory.review_status = "approved"
    store.upsert_memory(memory)

    second = export_task_outcome_candidates(
        harness_memory_db_path=db, vault_path=tmp_path / "vault", store=store, now=NOW
    )
    assert second.exported_count == 0
    assert second.skipped_count == 1
    unchanged = store.get_memory(memory_id)
    assert unchanged is not None
    assert unchanged.review_status == "approved"


def test_export_skips_non_outcome_and_empty_summary_rows(tmp_path: Path):
    db = tmp_path / "harness_memory.db"
    _harness_memory_db(
        db,
        [
            _task_outcome_row("task_1", memory_kind="document_learning"),
            _task_outcome_row("task_2", summary="   "),
        ],
    )
    store = MemoryGovernanceStore(tmp_path / "governed.db")

    result = export_task_outcome_candidates(
        harness_memory_db_path=db, vault_path=tmp_path / "vault", store=store, now=NOW
    )

    assert result.exported_count == 0
    assert result.skipped_count == 0
