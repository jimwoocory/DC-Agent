from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


def _load_case_plugin_module():
    module_path = (
        Path(__file__).resolve().parents[2]
        / "data"
        / "plugins"
        / "case_plugin"
        / "main.py"
    )
    spec = importlib.util.spec_from_file_location("dc_case_plugin_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _FakeContext:
    def __init__(self, nas_root: Path) -> None:
        self._config = {"case": {"nas_root": str(nas_root)}}

    def get_config(self):
        return self._config


class _FakeEvent:
    def __init__(self, session_id: str = "lark:plugin-room") -> None:
        self.message_str = ""
        self.unified_msg_origin = session_id
        self.result = None

    def set_result(self, result) -> None:
        self.result = result


async def test_case_plugin_archive_hook_writes_knowledge_sync_and_nas(
    tmp_path: Path,
) -> None:
    module = _load_case_plugin_module()
    module.__file__ = str(
        tmp_path / "repo" / "data" / "plugins" / "case_plugin" / "main.py"
    )
    nas_root = tmp_path / "nas"
    plugin = module.CasePlugin(_FakeContext(nas_root))
    await plugin.initialize()
    assert plugin.engine is not None

    case = await plugin.engine.create_case(
        name="Plugin Archive",
        platform_id="lark",
        session_id="lark:plugin-room",
        client_name="Acme",
        payload={"source": "case_plugin"},
    )
    case = await plugin.engine.attach_task(case.case_id, "task-plugin")
    await plugin.engine.add_deliverable(
        case.case_id,
        kind="summary",
        path="/tmp/plugin-summary.md",
    )

    archived = await plugin.engine.archive_case(case.case_id)

    data_dir = tmp_path / "repo" / "data"
    archive_path = data_dir / "case_archives" / f"{case.case_id}.md"
    records_path = data_dir / "case_archives" / "sync_records.jsonl"
    assert archived.status == "archived"
    assert archive_path.exists()
    assert records_path.exists()
    assert '"status": "synced"' in records_path.read_text(encoding="utf-8")
    assert list(nas_root.glob("dc-agent-cases/*/*/manifest.json"))


async def test_case_plugin_sync_status_reports_latest_record(
    tmp_path: Path,
) -> None:
    module = _load_case_plugin_module()
    module.__file__ = str(
        tmp_path / "repo" / "data" / "plugins" / "case_plugin" / "main.py"
    )
    plugin = module.CasePlugin(_FakeContext(tmp_path / "nas"))
    await plugin.initialize()
    assert plugin.engine is not None

    case = await plugin.engine.create_case(
        name="Plugin Sync Status",
        platform_id="lark",
        session_id="lark:plugin-status-room",
        payload={"source": "case_plugin"},
    )
    case = await plugin.engine.attach_task(case.case_id, "task-status")
    await plugin.engine.add_deliverable(
        case.case_id,
        kind="summary",
        path="/tmp/status-summary.md",
    )
    await plugin.engine.archive_case(case.case_id)
    event = _FakeEvent(session_id="lark:plugin-status-room")

    await plugin._case_sync_status(event, case.case_id[:12])

    assert event.result is not None
    text = event.result.get_plain_text()
    assert "Case 知识同步状态" in text
    assert "- status: synced" in text
    assert "- archive_path:" in text
    assert "- source_path:" in text
    assert "- deliverable_count: 1" in text
    assert "- task_count: 1" in text
    assert "- created_at:" in text


async def test_case_plugin_archive_hook_continues_when_knowledge_sync_crashes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_case_plugin_module()
    module.__file__ = str(
        tmp_path / "repo" / "data" / "plugins" / "case_plugin" / "main.py"
    )

    class CrashingKnowledgeSync:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def sync(self, *args, **kwargs):
            raise RuntimeError("sync boom")

    monkeypatch.setattr(module, "CaseKnowledgeSync", CrashingKnowledgeSync)
    nas_root = tmp_path / "nas"
    plugin = module.CasePlugin(_FakeContext(nas_root))
    await plugin.initialize()
    assert plugin.engine is not None

    case = await plugin.engine.create_case(
        name="Plugin Archive With Sync Failure",
        platform_id="lark",
        session_id="lark:plugin-failure-room",
    )

    archived = await plugin.engine.archive_case(case.case_id)

    assert archived.status == "archived"
    assert list(nas_root.glob("dc-agent-cases/*/*/manifest.json"))


async def test_case_plugin_sync_status_handles_missing_and_malformed_records(
    tmp_path: Path,
) -> None:
    module = _load_case_plugin_module()
    module.__file__ = str(
        tmp_path / "repo" / "data" / "plugins" / "case_plugin" / "main.py"
    )
    plugin = module.CasePlugin(_FakeContext(tmp_path / "nas"))
    await plugin.initialize()
    assert plugin.knowledge_sync is not None
    plugin.knowledge_sync.records_path.parent.mkdir(parents=True, exist_ok=True)
    plugin.knowledge_sync.records_path.write_text(
        "{bad json\n",
        encoding="utf-8",
    )
    event = _FakeEvent()

    await plugin._case_sync_status(event, "missing-case")

    assert event.result is not None
    assert "未找到 case missing-case 的知识同步记录" in event.result.get_plain_text()


async def test_case_plugin_sync_status_displays_failed_record_error(
    tmp_path: Path,
) -> None:
    module = _load_case_plugin_module()
    module.__file__ = str(
        tmp_path / "repo" / "data" / "plugins" / "case_plugin" / "main.py"
    )
    plugin = module.CasePlugin(_FakeContext(tmp_path / "nas"))
    await plugin.initialize()
    assert plugin.knowledge_sync is not None
    plugin.knowledge_sync.records_path.parent.mkdir(parents=True, exist_ok=True)
    plugin.knowledge_sync.records_path.write_text(
        json.dumps(
            {
                "case_id": "failed-case-123",
                "status": "failed",
                "archive_path": str(tmp_path / "case_archives" / "failed.md"),
                "source_path": str(tmp_path / "cases.db"),
                "task_ids": ["task-a"],
                "deliverable_count": 3,
                "error": "markdown write failed",
                "created_at": "2026-06-05T00:00:00+00:00",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    event = _FakeEvent()

    await plugin._case_sync_status(event, "failed-case")

    assert event.result is not None
    text = event.result.get_plain_text()
    assert "- status: failed" in text
    assert "- error: markdown write failed" in text
    assert "- deliverable_count: 3" in text
    assert "- task_count: 1" in text
