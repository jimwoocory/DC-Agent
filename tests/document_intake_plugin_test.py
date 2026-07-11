from unittest.mock import AsyncMock

import pytest

from astrbot.api.message_components import File
from data.plugins.document_intake_plugin.main import (
    DocumentIntakeCardHandle,
    DocumentIntakePlugin,
    IntakeResult,
    _build_context_block,
    _copy_component_to_inbox,
    _finalize_document_intake_card,
    _requests_kb_import,
)


@pytest.mark.asyncio
async def test_copy_component_to_inbox_parses_text_file(tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("部门 SOP：每天 10 点前提交日报。", encoding="utf-8")
    inbox = tmp_path / "nas" / "knowledge" / "inbox" / "download"
    component = File(name="部门 SOP.txt", file=str(source))

    result = await _copy_component_to_inbox(
        component,
        inbox_dir=inbox,
        supported_suffixes={".txt"},
        max_file_mb=1,
    )

    assert result.status == "parsed"
    assert result.stored_path.parent == inbox
    assert result.stored_path.exists()
    assert result.stored_path.read_text(encoding="utf-8") == source.read_text(
        encoding="utf-8"
    )
    assert "每天 10 点前提交日报" in result.parsed_text
    assert result.sha256


@pytest.mark.asyncio
async def test_upload_results_to_kb_preserves_nas_source_path(tmp_path):
    stored = tmp_path / "nas" / "knowledge" / "inbox" / "download" / "plan.txt"
    stored.parent.mkdir(parents=True)
    stored.write_text("方案内容", encoding="utf-8")

    helper = AsyncMock()
    kb_manager = AsyncMock()
    kb_manager.get_kb_by_name.return_value = helper
    context = type("Context", (), {"kb_manager": kb_manager})()

    plugin = DocumentIntakePlugin(
        context,
        {
            "kb_names": ["nas_knowledge"],
            "inbox_dir": str(stored.parent),
        },
    )
    result = IntakeResult(
        original_name="plan.txt",
        stored_path=stored,
        sha256="abc",
        size_bytes=stored.stat().st_size,
        parsed_text="方案内容",
        status="parsed",
    )

    await plugin._upload_results_to_kb([result])

    helper.upload_document.assert_awaited_once()
    _, kwargs = helper.upload_document.call_args
    assert kwargs["file_name"] == "plan.txt"
    assert kwargs["file_type"] == "txt"
    assert kwargs["source_path"] == str(stored)
    assert kwargs["file_content"] == stored.read_bytes()


def test_build_context_block_contains_document_excerpt(tmp_path):
    result = IntakeResult(
        original_name="training.txt",
        stored_path=tmp_path / "training.txt",
        sha256="sha",
        size_bytes=12,
        parsed_text="新人培训第一步：先确认部门、岗位、当前任务。",
        status="parsed",
    )

    block = _build_context_block([result], 500)

    assert block.startswith("<dc_document_intake>")
    assert "training.txt" in block
    assert "新人培训第一步" in block


def test_document_intake_requires_explicit_knowledge_base_intent() -> None:
    assert _requests_kb_import("帮我总结这两份方案") is False
    assert _requests_kb_import("把这两份方案归档到知识库") is True


@pytest.mark.asyncio
async def test_finalize_document_intake_card_uses_runtime_card(tmp_path):
    class Streamer:
        def __init__(self) -> None:
            self.finalized = []

        async def finalize(self, message_id, card):
            self.finalized.append((message_id, card))
            return True

    streamer = Streamer()
    handle = DocumentIntakeCardHandle(
        streamer=streamer,
        message_id="om_doc",
        platform_id="lark-test",
    )
    result = IntakeResult(
        original_name="training.txt",
        stored_path=tmp_path / "training.txt",
        sha256="sha",
        size_bytes=12,
        parsed_text="培训内容",
        status="parsed",
    )

    ok = await _finalize_document_intake_card(
        handle,
        results=[result],
        kb_summary={
            "kb_name": "nas_knowledge",
            "imported_count": 1,
            "failed_count": 0,
        },
        inbox_path=tmp_path,
        auto_import=True,
    )

    assert ok is True
    assert streamer.finalized[0][0] == "om_doc"
    card = streamer.finalized[0][1]
    assert card["header"]["title"]["content"] == "文档上传 · 已入库"
