from __future__ import annotations

import sqlite3

from dc_engines.creative_memory import search_company_creative_memory


def _creative_memory_db(tmp_path) -> str:
    """Create a minimal NAS memory index for creative retrieval tests.

    Args:
        tmp_path: Pytest temporary directory.

    Returns:
        Path to the SQLite fixture.
    """
    db_path = tmp_path / "nas_memory.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE documents (
                doc_key TEXT PRIMARY KEY,
                rel_path TEXT NOT NULL,
                source_path TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                file_size INTEGER NOT NULL,
                parser TEXT NOT NULL,
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                tags_json TEXT NOT NULL,
                indexed_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                archive_path TEXT DEFAULT '',
                project_id TEXT DEFAULT '',
                project_name TEXT DEFAULT '',
                doc_type TEXT DEFAULT '',
                initiator TEXT DEFAULT '',
                owner TEXT DEFAULT '',
                departments_json TEXT DEFAULT '[]',
                participants_json TEXT DEFAULT '[]',
                confidence REAL DEFAULT 0,
                review_status TEXT DEFAULT 'need_review'
            );
            CREATE TABLE chunks (
                chunk_id TEXT PRIMARY KEY,
                doc_key TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                text TEXT NOT NULL
            );
            """
        )
        fixtures = (
            (
                "approved-copy",
                "ObsidianVault/品牌文案/五菱之光EV春节传播.md",
                "五菱之光EV春节传播文案",
                "五菱之光EV春节用户传播的正式历史文案",
                "文案素材",
                "confirmed",
                "五菱之光EV春节文案以返乡路上的真实用户故事为核心，品牌表达克制温暖。",
            ),
            (
                "draft-copy",
                "ObsidianVault/品牌文案/五菱之光EV用户故事草稿.md",
                "五菱之光EV用户故事草稿",
                "待复核的五菱之光EV春节内容草稿",
                "传播策略",
                "need_review",
                "五菱之光EV春节文案草稿建议从车主收拾年货的生活细节切入。",
            ),
            (
                "quotation",
                "ObsidianVault/供应商价格库/车辆拍摄报价.md",
                "五菱之光EV春节拍摄报价",
                "供应商历史报价",
                "资料文档",
                "confirmed",
                "五菱之光EV春节文案配套拍摄报价：单价待定，不含税总价待定，材质工艺与供应商待确认。",
            ),
        )
        for index, fixture in enumerate(fixtures, start=1):
            doc_key, rel_path, title, summary, doc_type, review_status, text = fixture
            conn.execute(
                """
                INSERT INTO documents (
                    doc_key, rel_path, source_path, sha256, file_size, parser,
                    title, summary, tags_json, indexed_at, metadata_json,
                    doc_type, review_status
                ) VALUES (?, ?, ?, '', 1, 'md', ?, ?, '[]', '2026-07-16', '{}', ?, ?)
                """,
                (
                    doc_key,
                    rel_path,
                    f"/vault/{rel_path}",
                    title,
                    summary,
                    doc_type,
                    review_status,
                ),
            )
            conn.execute(
                "INSERT INTO chunks VALUES (?, ?, 0, ?)",
                (f"chunk-{index}", doc_key, text),
            )
    return str(db_path)


def test_creative_memory_returns_bounded_obsidian_originals(tmp_path) -> None:
    evidence = search_company_creative_memory(
        "五菱之光EV春节文案",
        db_path=_creative_memory_db(tmp_path),
        limit=3,
    )

    assert len(evidence) == 2
    assert {item["title"] for item in evidence} == {
        "五菱之光EV春节传播文案",
        "五菱之光EV用户故事草稿",
    }
    confirmed = next(item for item in evidence if item["source_status"] == "已复核")
    pending = next(item for item in evidence if item["source_status"] == "待复核")
    assert confirmed["usage_policy"] == "facts_and_style"
    assert "真实用户故事" in confirmed["excerpt"]
    assert pending["usage_policy"] == "style_reference_only"
    assert pending["source_path"].endswith("五菱之光EV用户故事草稿.md")
    assert all("报价" not in item["title"] for item in evidence)


def test_creative_memory_returns_empty_when_index_is_not_mounted(tmp_path) -> None:
    assert (
        search_company_creative_memory(
            "五菱之光EV春节文案",
            db_path=tmp_path / "missing.db",
        )
        == []
    )
