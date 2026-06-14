from __future__ import annotations

import json
from pathlib import Path

from dc_engines.case.case_store import CaseStore
from dc_engines.case.contracts import Case
from dc_engines.case.engine import CaseEngine
from dc_engines.case.knowledge_sync import CaseKnowledgeSync


def _read_records(records_path: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in records_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


async def test_case_knowledge_sync_writes_markdown_and_record(
    case_store: CaseStore,
    tmp_path: Path,
) -> None:
    engine = CaseEngine(case_store, archive_hook=lambda _case: None)
    case = await engine.create_case(
        name="Proposal Archive",
        platform_id="lark",
        session_id="lark:case-room",
        client_name="Acme",
        payload={"requester": "Ada", "priority": "high"},
    )
    case = await engine.attach_task(case.case_id, "task-1")
    case = await engine.attach_task(case.case_id, "task-2")
    case = await engine.add_deliverable(
        case.case_id,
        kind="brief",
        path="/tmp/brief.md",
        extra={"title": "Discovery brief"},
    )

    sync = CaseKnowledgeSync(
        archive_root=tmp_path / "case_archives",
        records_path=tmp_path / "case_archives" / "sync_records.jsonl",
    )

    record = sync.sync(case, source_path=case_store.db_path)

    archive_path = Path(record.archive_path)
    assert record.status == "synced"
    assert archive_path == tmp_path / "case_archives" / f"{case.case_id}.md"
    assert archive_path.exists()
    markdown = archive_path.read_text(encoding="utf-8")
    assert "# Proposal Archive" in markdown
    assert f"case_id: {case.case_id}" in markdown
    assert "client_name: Acme" in markdown
    assert "platform_id: lark" in markdown
    assert "session_id: lark:case-room" in markdown
    assert "- task-1" in markdown
    assert "- task-2" in markdown
    assert '"path": "/tmp/brief.md"' in markdown
    assert '"priority": "high"' in markdown
    assert "generated_at:" in markdown

    records = _read_records(sync.records_path)
    assert records == [
        {
            "archive_path": str(archive_path),
            "case_id": case.case_id,
            "created_at": record.created_at,
            "deliverable_count": 1,
            "error": None,
            "source_path": str(Path(case_store.db_path)),
            "status": "synced",
            "task_ids": ["task-1", "task-2"],
        }
    ]


async def test_case_knowledge_sync_records_failed_write(
    case_store: CaseStore,
    tmp_path: Path,
    monkeypatch,
) -> None:
    case = await case_store.create_case(
        name="Failing Archive",
        platform_id="lark",
        session_id="lark:failure-room",
        case_id="case-failure",
    )
    sync = CaseKnowledgeSync(
        archive_root=tmp_path / "case_archives",
        records_path=tmp_path / "case_archives" / "sync_records.jsonl",
    )

    def fail_write(_case: Case, _archive_path: Path, _generated_at: str) -> None:
        raise OSError("simulated markdown write failure")

    monkeypatch.setattr(sync, "_write_archive_markdown", fail_write)

    record = sync.sync(case, source_path=case_store.db_path)

    assert record.status == "failed"
    assert record.error == "simulated markdown write failure"
    assert not Path(record.archive_path).exists()

    records = _read_records(sync.records_path)
    assert len(records) == 1
    assert records[0]["case_id"] == "case-failure"
    assert records[0]["status"] == "failed"
    assert records[0]["error"] == "simulated markdown write failure"
    assert records[0]["source_path"] == str(Path(case_store.db_path))


async def test_case_knowledge_sync_sanitizes_custom_case_id_filename(
    case_store: CaseStore,
    tmp_path: Path,
) -> None:
    unsafe_case_id = "../unsafe\\case/id\x01"
    case = await case_store.create_case(
        name="Unsafe ID Archive",
        platform_id="lark",
        session_id="lark:unsafe-room",
        case_id=unsafe_case_id,
    )
    archive_root = tmp_path / "case_archives"
    sync = CaseKnowledgeSync(
        archive_root=archive_root,
        records_path=archive_root / "sync_records.jsonl",
    )

    record = sync.sync(case, source_path=case_store.db_path)

    archive_path = Path(record.archive_path)
    assert record.status == "synced"
    assert record.case_id == unsafe_case_id
    assert archive_path.exists()
    assert archive_path.parent == archive_root
    assert archive_path.relative_to(archive_root).parts == ("unsafe_case_id.md",)
    records = _read_records(sync.records_path)
    assert records[0]["case_id"] == unsafe_case_id


async def test_case_engine_default_archive_hook_syncs_without_runtime_data(
    case_store: CaseStore,
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    engine = CaseEngine(case_store)
    case = await engine.create_case(
        name="Engine Archive",
        platform_id="lark",
        session_id="lark:engine-room",
        client_name="Beta",
        payload={"channel": "case-plugin"},
    )
    case = await engine.attach_task(case.case_id, "task-engine")
    await engine.add_deliverable(
        case.case_id,
        kind="summary",
        path="/tmp/summary.md",
    )

    archived = await engine.archive_case(case.case_id)

    archive_root = tmp_path / "data" / "case_archives"
    archive_path = archive_root / f"{case.case_id}.md"
    records_path = archive_root / "sync_records.jsonl"
    assert archived.status == "archived"
    assert archive_path.exists()
    records = _read_records(records_path)
    assert records[-1]["status"] == "synced"
    assert records[-1]["case_id"] == case.case_id
    assert records[-1]["task_ids"] == ["task-engine"]
    assert records[-1]["deliverable_count"] == 1


def test_engine_no_longer_contains_kb_sync_stub() -> None:
    engine_source = Path("dc_engines/dc_engines/case/engine.py").read_text(
        encoding="utf-8"
    )

    assert "kb-sync stub" not in engine_source


def test_case_knowledge_sync_latest_record_skips_malformed_jsonl(
    tmp_path: Path,
) -> None:
    records_path = tmp_path / "case_archives" / "sync_records.jsonl"
    records_path.parent.mkdir(parents=True)
    records_path.write_text(
        "\n".join(
            [
                '{"case_id": "case-one", "status": "synced", '
                '"archive_path": "/old.md", "source_path": "/cases.db", '
                '"task_ids": ["task-1"], "deliverable_count": 1, '
                '"error": null, "created_at": "2026-06-01T00:00:00+00:00"}',
                "{not valid json",
                '{"case_id": "case-one", "status": "failed", '
                '"archive_path": "/new.md", "source_path": "/cases.db", '
                '"task_ids": ["task-1", "task-2"], "deliverable_count": 2, '
                '"error": "sync failed", '
                '"created_at": "2026-06-02T00:00:00+00:00"}',
                '{"case_id": "case-two", "status": "synced", '
                '"archive_path": "/two.md", "source_path": "/cases.db", '
                '"task_ids": [], "deliverable_count": 0, '
                '"error": null, "created_at": "2026-06-03T00:00:00+00:00"}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    sync = CaseKnowledgeSync(records_path=records_path)

    record = sync.latest_record_for_case("case-one")

    assert record is not None
    assert record.status == "failed"
    assert record.archive_path == "/new.md"
    assert record.error == "sync failed"
    assert len(record.task_ids) == 2
    assert sync.latest_record_for_case("case-") is None
