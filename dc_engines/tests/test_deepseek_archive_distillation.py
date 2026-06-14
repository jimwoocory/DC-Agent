from __future__ import annotations

import json
from pathlib import Path

from dc_engines.assistant_distillation import AssistantDistillationStore
from dc_engines.deepseek_archive_distillation import export_deepseek_archive_handoff
from dc_engines.memory_governance.store import MemoryGovernanceStore


def test_deepseek_handoff_exports_monthly_work_digest_without_think_or_personal(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "conversations.json"
    archive.write_text(
        json.dumps(
            [
                _conversation(
                    "work_1",
                    "五菱星光S短视频脚本",
                    "2025-09-03T10:00:00+08:00",
                    "我要给五菱星光S写竖屏短视频脚本，减少纯口播，轻量化拍摄",
                    "脚本方案",
                    think="内部推理不要入库",
                ),
                _conversation(
                    "personal_1",
                    "鼻窦炎解析",
                    "2026-01-04T10:00:00+08:00",
                    "请帮我分析鼻窦炎报告",
                    "医学建议",
                ),
                _conversation(
                    "old_1",
                    "八字大师",
                    "2025-08-27T10:00:00+08:00",
                    "你是八字大师",
                    "占卜结果",
                ),
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    distill_store = AssistantDistillationStore(tmp_path / "distill.db")
    governance_store = MemoryGovernanceStore(tmp_path / "governed.db")

    summary = export_deepseek_archive_handoff(
        archive_path=archive,
        distillation_store=distill_store,
        governance_store=governance_store,
        vault_path=tmp_path / "ObsidianVault",
        now="2026-06-05T12:00:00Z",
    )

    assert summary.scanned_conversations == 3
    assert summary.eligible_conversations == 1
    assert summary.exported_memories == 1
    memory = governance_store.get_memory(summary.memory_ids[0])
    assert memory is not None
    assert memory.review_status == "need_review"
    assert memory.source_system == "conversation"
    assert "department:执行部影视编导" in memory.tags
    assert "五菱星光S短视频脚本" in memory.canonical_text
    assert "内部推理不要入库" not in memory.canonical_text
    assert "鼻窦炎" not in memory.canonical_text
    note = summary.note_paths[0].read_text(encoding="utf-8")
    assert "review_status: need_review" in note
    assert "DeepSeek Archive Digest - 执行部影视编导/2025-09" in note


def test_deepseek_handoff_generates_pending_content_workflow_candidates(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "conversations.json"
    archive.write_text(
        json.dumps(
            [
                _conversation(
                    "work_1",
                    "轻量化脚本一",
                    "2025-09-03T10:00:00+08:00",
                    "视频要求竖屏，减少纯口播，拍摄要轻量化好实现",
                    "方案",
                ),
                _conversation(
                    "work_2",
                    "轻量化脚本二",
                    "2025-09-04T10:00:00+08:00",
                    "官方账号发布，减少纯口播，拍摄要轻量化好实现",
                    "方案",
                ),
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    distill_store = AssistantDistillationStore(tmp_path / "distill.db")
    governance_store = MemoryGovernanceStore(tmp_path / "governed.db")

    summary = export_deepseek_archive_handoff(
        archive_path=archive,
        distillation_store=distill_store,
        governance_store=governance_store,
        vault_path=tmp_path / "ObsidianVault",
        min_pattern_count=2,
        now="2026-06-05T12:00:00Z",
    )

    assert summary.assistant_candidates == 1
    candidates = distill_store.list_candidates(status="pending", limit=10)
    assert len(candidates) == 1
    assert candidates[0].kind == "tone_template"
    assert candidates[0].template_name == "deepseek_archive_content_director_workflow"
    assert "轻量化" in candidates[0].template_body


def _conversation(
    conversation_id: str,
    title: str,
    inserted_at: str,
    request: str,
    response: str,
    *,
    think: str = "",
) -> dict:
    fragments = [{"type": "REQUEST", "content": request}]
    if think:
        fragments.append({"type": "THINK", "content": think})
    fragments.append({"type": "RESPONSE", "content": response})
    return {
        "id": conversation_id,
        "title": title,
        "inserted_at": inserted_at,
        "updated_at": inserted_at,
        "mapping": {
            "root": {"id": "root", "parent": None, "children": ["1"], "message": None},
            "1": {
                "id": "1",
                "parent": "root",
                "children": [],
                "message": {
                    "files": [],
                    "model": "deepseek-chat",
                    "inserted_at": inserted_at,
                    "fragments": fragments,
                },
            },
        },
    }
