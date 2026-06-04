"""Employee memory bridge tests."""

from __future__ import annotations

from pathlib import Path

from dc_engines.employee_directory import Employee, EmployeeMemory, EmployeeMemoryBridge


def _memory(kind: str, content: str) -> EmployeeMemory:
    return EmployeeMemory(
        memory_id=f"m_{kind}",
        open_id="ou_secret",
        kind=kind,  # type: ignore[arg-type]
        content=content,
        relevance=0.8,
        created_at="2026-05-20T00:00:00+00:00",
    )


def test_archive_document_is_sanitized_and_filters_private_memory() -> None:
    bridge = EmployeeMemoryBridge()
    emp = Employee(
        open_id="ou_secret",
        display_name="杨国民",
        department="管理层",
        role="总经理",
        personality_summary="结论导向；依据 3 条对话证据生成。",
        communication_style="称呼固定为杨总，多使用敬语和「您」。",
    )
    document = bridge.build_archive_document(
        emp,
        [
            _memory("preference", "preference: 喜欢结论先行（出自：原始私聊内容）"),
            _memory("fact", "fact: explicit_correction preferred_address=杨总"),
            _memory("persona_evidence", "persona_evidence: sample=原始私聊"),
        ],
        preferred_address="杨总",
        relation_type="boss",
    )

    assert "ou_secret" not in document
    assert "原始私聊内容" not in document
    assert "preference: 喜欢结论先行" in document
    assert "explicit_correction" not in document
    assert "persona_evidence" not in document


def test_export_to_nas_writes_controlled_markdown(tmp_path: Path) -> None:
    bridge = EmployeeMemoryBridge()
    result = bridge.export_to_nas(
        "# 员工协作画像\n",
        file_name="employee-memory-test.md",
        inbox_dir=tmp_path,
    )

    target = tmp_path / "employee_memory_identity" / "employee-memory-test.md"
    assert result.status == "synced"
    assert target.read_text(encoding="utf-8") == "# 员工协作画像\n"


async def test_sync_to_astrbot_kb_uploads_prechunked_document() -> None:
    class FakeHelper:
        def __init__(self) -> None:
            self.calls = []

        async def upload_document(self, **kwargs):
            self.calls.append(kwargs)

    class FakeKBManager:
        def __init__(self) -> None:
            self.helper = FakeHelper()

        async def get_kb_by_name(self, name: str):
            if name == "nas_knowledge":
                return self.helper
            return None

    manager = FakeKBManager()
    bridge = EmployeeMemoryBridge(kb_names=("missing", "nas_knowledge"))

    result = await bridge.sync_to_astrbot_kb(
        manager,
        "# 员工协作画像\n",
        file_name="employee-memory-test.md",
    )

    assert result.status == "synced"
    assert result.target == "nas_knowledge"
    assert manager.helper.calls[0]["file_name"] == "employee-memory-test.md"
    assert manager.helper.calls[0]["pre_chunked_text"] == ["# 员工协作画像\n"]
