from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path


def load_cli_module():
    script = Path("scripts-tools/obsidian_memory_governance.py")
    spec = importlib.util.spec_from_file_location("obsidian_memory_governance_cli", script)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def create_nas_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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
                "launch.md",
                "/Users/dianchi/nas_kb/launch.md",
                "abc",
                "md",
                "Launch SOP",
                "Launch SOP is approved by marketing.",
                '["launch"]',
                "2026-06-04T00:00:00Z",
                "{}",
                "launch",
                "Launch",
                "SOP",
                "谭媛尹",
                0.88,
                "need_review",
            ),
        )


def test_cli_doctor_and_status(tmp_path: Path) -> None:
    cli = load_cli_module()
    (tmp_path / "ObsidianVault").mkdir()
    create_nas_db(tmp_path / "data" / "nas_memory.db")
    (tmp_path / "dc_engines" / "dc_engines" / "memory_governance").mkdir(
        parents=True
    )

    doctor = cli.command_doctor(tmp_path)
    status = cli.command_status(tmp_path)

    assert doctor["ok"] is True
    assert status["ok"] is True
    assert status["total"] == 0


def test_cli_export_import_promote_dry_run(tmp_path: Path) -> None:
    cli = load_cli_module()
    (tmp_path / "ObsidianVault").mkdir()
    create_nas_db(tmp_path / "data" / "nas_memory.db")

    exported = cli.command_export(tmp_path, limit=10)
    note_path = Path(exported["note_paths"][0])
    markdown = note_path.read_text(encoding="utf-8")
    markdown = markdown.replace("review_status: need_review", "review_status: approved")
    markdown = markdown.replace("reviewer: ''", "reviewer: dianchi")
    note_path.write_text(markdown, encoding="utf-8")
    imported = cli.command_import(tmp_path, actor="test")
    promoted = cli.command_promote(tmp_path, actor="test", dry_run=True)

    assert exported["exported_count"] == 1
    assert imported["imported_count"] == 1
    assert imported["decision_count"] == 1
    assert promoted["dry_run"] is True
    assert promoted["promoted_memory_ids"] == exported["memory_ids"]
    assert not (tmp_path / "data" / "config" / "nas_memory_overrides.json").exists()
