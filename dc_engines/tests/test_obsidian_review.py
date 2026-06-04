from __future__ import annotations

import json
from pathlib import Path

from dc_engines.obsidian_review import (
    append_review_record,
    build_review_record,
    load_review_records,
    looks_like_obsidian_review_reply,
    parse_review_reply,
)


def test_parse_obsidian_review_correction_reply() -> None:
    text = """需要调整：
- 项目/资料名：星光S
- 负责人：玉晓莉
- 文档类型：文案素材
- 备注：这是测试确认
"""

    action, fields = parse_review_reply(text)

    assert looks_like_obsidian_review_reply(text)
    assert action == "correction"
    assert fields["project_or_document"] == "星光S"
    assert fields["owner"] == "玉晓莉"
    assert fields["doc_type"] == "文案素材"


def test_build_and_append_review_record_with_memory_candidate(tmp_path: Path) -> None:
    context = {
        "documents": [
            {
                "doc_key": "doc_1",
                "title": "星光S",
                "rel_path": "星光S.xlsx",
                "project_name": "星光S",
                "doc_type": "文案素材",
                "owner": "",
                "review_status": "need_review",
            },
            {
                "doc_key": "doc_2",
                "title": "已确认资料",
                "review_status": "confirmed",
            },
        ]
    }

    record = build_review_record(
        text="确认无误",
        sender_id="ou_1",
        sender_name="玉晓莉",
        session_id="s1",
        platform_id="巅池-Agent小助手",
        memory_context=context,
    )
    path = tmp_path / "confirmations.jsonl"
    append_review_record(record, path)

    rows = load_review_records(path)
    assert len(rows) == 1
    assert rows[0]["action"] == "confirm"
    assert rows[0]["candidates"][0]["doc_key"] == "doc_1"
    assert json.loads(path.read_text(encoding="utf-8"))["sender_name"] == "玉晓莉"
