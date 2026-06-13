from __future__ import annotations

import json
from pathlib import Path

from dc_engines.chat_archive import LarkChatArchiveRecord, append_lark_chat_record


def _record(**overrides: object) -> LarkChatArchiveRecord:
    values = {
        "timestamp": "2026-06-05T09:30:00+08:00",
        "platform_id": "lark",
        "conversation_id": "oc_123",
        "message_id": "om_456",
        "sender_type": "user",
        "feishu_open_id": "ou_abc",
        "employee_name": "张三",
        "department": "产品部",
        "text": "你好，帮我整理长期归档。",
        "provider_model": "gpt-5",
        "total_tokens": 128,
        "metadata": {"source": "unit-test", "priority": 1},
    }
    values.update(overrides)
    return LarkChatArchiveRecord(**values)


def test_append_lark_chat_record_writes_chinese_path_jsonl_and_markdown(
    tmp_path: Path,
) -> None:
    record = _record()

    result = append_lark_chat_record(tmp_path, record)

    expected_dir = (
        tmp_path
        / "knowledge"
        / "Chat"
        / "Lark Chat"
        / "产品部"
        / "张三"
        / "2026"
        / "06"
    )
    assert result.jsonl_path == expected_dir / "2026-06-05.jsonl"
    assert result.markdown_path == expected_dir / "2026-06-05.md"
    assert len(result.text_hash) == 64

    json_lines = result.jsonl_path.read_text(encoding="utf-8").splitlines()
    assert len(json_lines) == 1
    payload = json.loads(json_lines[0])
    assert payload["employee_name"] == "张三"
    assert payload["department"] == "产品部"
    assert payload["text"] == "你好，帮我整理长期归档。"
    assert payload["provider_model"] == "gpt-5"
    assert payload["total_tokens"] == 128
    assert payload["metadata"] == {"priority": 1, "source": "unit-test"}
    assert payload["text_hash"] == result.text_hash
    assert "\\u4ea7" not in json_lines[0]

    markdown = result.markdown_path.read_text(encoding="utf-8")
    assert "## 2026-06-05T09:30:00+08:00 [user]" in markdown
    assert "- Employee: 张三" in markdown
    assert "- Department: 产品部" in markdown
    assert "- Model: gpt-5" in markdown
    assert "- Total Tokens: 128" in markdown
    assert "你好，帮我整理长期归档。" in markdown


def test_append_lark_chat_record_sanitizes_slashes_in_path_segments(
    tmp_path: Path,
) -> None:
    record = _record(department="  研发/平台\\架构  ", employee_name=" 李/四\\QA ")

    result = append_lark_chat_record(tmp_path, record)

    assert "研发／平台＼架构" in result.jsonl_path.parts
    assert "李／四＼QA" in result.jsonl_path.parts
    assert "/" not in result.jsonl_path.parts[-5]
    assert "\\" not in result.jsonl_path.parts[-4]


def test_append_lark_chat_record_uses_unknown_fallbacks(tmp_path: Path) -> None:
    record = _record(department="  ", employee_name="")

    result = append_lark_chat_record(tmp_path, record)

    assert "未知部门" in result.jsonl_path.parts
    assert "未知员工" in result.jsonl_path.parts
    markdown = result.markdown_path.read_text(encoding="utf-8")
    assert "- Employee: 未知员工" in markdown
    assert "- Department: 未知部门" in markdown


def test_append_lark_chat_record_does_not_dedupe_duplicates(tmp_path: Path) -> None:
    record = _record(message_id="same-message")

    first = append_lark_chat_record(tmp_path, record)
    second = append_lark_chat_record(tmp_path, record)

    assert first.jsonl_path == second.jsonl_path
    json_lines = first.jsonl_path.read_text(encoding="utf-8").splitlines()
    assert len(json_lines) == 2
    assert [json.loads(line)["message_id"] for line in json_lines] == [
        "same-message",
        "same-message",
    ]
    markdown = first.markdown_path.read_text(encoding="utf-8")
    assert markdown.count("## 2026-06-05T09:30:00+08:00 [user]") == 2
