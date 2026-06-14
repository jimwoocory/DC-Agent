from __future__ import annotations

import json
from pathlib import Path

from dc_engines.assistant_distillation import AssistantDistillationStore
from dc_engines.lark_archive_distillation import export_lark_archive_handoff
from dc_engines.memory_governance.store import MemoryGovernanceStore


def _append_records(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
        encoding="utf-8",
    )


def test_lark_archive_handoff_exports_governed_memory_note(tmp_path: Path) -> None:
    archive_file = (
        tmp_path
        / "nas"
        / "knowledge"
        / "Chat"
        / "Lark Chat"
        / "业务部"
        / "蔡挺"
        / "2026"
        / "06"
        / "2026-06-05.jsonl"
    )
    _append_records(
        archive_file,
        [
            _record("user", "最近柳州汛期来了，帮我做出行提醒", department="业务部"),
            _record("assistant", "建议避开低洼路段，提前规划路线", department="业务部"),
        ],
    )
    distill_store = AssistantDistillationStore(tmp_path / "distill.db")
    governance_store = MemoryGovernanceStore(tmp_path / "governed.db")

    summary = export_lark_archive_handoff(
        archive_root=tmp_path / "nas",
        distillation_store=distill_store,
        governance_store=governance_store,
        vault_path=tmp_path / "ObsidianVault",
        now="2026-06-05T12:00:00Z",
    )

    assert summary.scanned_files == 1
    assert summary.scanned_records == 2
    assert summary.exported_memories == 1
    assert summary.assistant_candidates == 0
    memory = governance_store.get_memory(summary.memory_ids[0])
    assert memory is not None
    assert memory.review_status == "need_review"
    assert memory.source_system == "conversation"
    assert memory.source_path == str(archive_file)
    assert "department:业务部" in memory.tags
    note = summary.note_paths[0].read_text(encoding="utf-8")
    assert "review_status: need_review" in note
    assert "最近柳州汛期来了" in note


def test_lark_archive_handoff_generates_assistant_candidates(
    tmp_path: Path,
) -> None:
    archive_file = (
        tmp_path
        / "nas"
        / "knowledge"
        / "Chat"
        / "Lark Chat"
        / "运营部"
        / "李四"
        / "2026"
        / "06"
        / "2026-06-05.jsonl"
    )
    _append_records(
        archive_file,
        [
            _record("user", "你这个语气太官方了，短一点"),
            _record("user", "刚刚那句有点生硬，帮我拉客户进群"),
            _record("assistant", "我调整一下。"),
        ],
    )
    distill_store = AssistantDistillationStore(tmp_path / "distill.db")
    governance_store = MemoryGovernanceStore(tmp_path / "governed.db")

    summary = export_lark_archive_handoff(
        archive_root=tmp_path / "nas" / "knowledge" / "Chat" / "Lark Chat",
        distillation_store=distill_store,
        governance_store=governance_store,
        vault_path=tmp_path / "ObsidianVault",
        min_feedback_count=1,
        now="2026-06-05T12:00:00Z",
    )

    assert summary.assistant_candidates == 3
    candidates = distill_store.list_candidates(status="pending", limit=10)
    assert {candidate.kind for candidate in candidates} == {
        "intent_alias",
        "tone_template",
    }
    assert any(
        candidate.template_name == "lark_archive_employee_tone_feedback"
        for candidate in candidates
    )
    assert any("拉" in candidate.pattern for candidate in candidates)
    assert any("短一点" in candidate.pattern for candidate in candidates)


def test_lark_archive_handoff_skips_already_reviewed_memory(
    tmp_path: Path,
) -> None:
    archive_file = (
        tmp_path
        / "nas"
        / "knowledge"
        / "Chat"
        / "Lark Chat"
        / "业务部"
        / "蔡挺"
        / "2026"
        / "06"
        / "2026-06-05.jsonl"
    )
    _append_records(archive_file, [_record("user", "记一下：我负责柳州项目")])
    distill_store = AssistantDistillationStore(tmp_path / "distill.db")
    governance_store = MemoryGovernanceStore(tmp_path / "governed.db")

    first = export_lark_archive_handoff(
        archive_root=tmp_path / "nas",
        distillation_store=distill_store,
        governance_store=governance_store,
        vault_path=tmp_path / "ObsidianVault",
        now="2026-06-05T12:00:00Z",
    )
    memory = governance_store.get_memory(first.memory_ids[0])
    assert memory is not None
    memory.review_status = "approved"
    memory.approved_by = "ops"
    memory.approved_at = "2026-06-05T13:00:00Z"
    governance_store.upsert_memory(memory)

    second = export_lark_archive_handoff(
        archive_root=tmp_path / "nas",
        distillation_store=distill_store,
        governance_store=governance_store,
        vault_path=tmp_path / "ObsidianVault",
        now="2026-06-05T14:00:00Z",
    )

    assert second.exported_memories == 0
    assert second.skipped_memories == 1


def _record(
    sender_type: str,
    text: str,
    *,
    department: str = "运营部",
    employee_name: str = "李四",
) -> dict:
    return {
        "timestamp": "2026-06-05T10:00:00+08:00",
        "platform_id": "巅池-Agent小助手",
        "conversation_id": "conv_1",
        "message_id": "msg_1",
        "sender_type": sender_type,
        "feishu_open_id": "ou_1",
        "employee_name": employee_name,
        "department": department,
        "text": text,
        "metadata": {"source": "test"},
    }
