import json
from pathlib import Path

CONTRACT = Path("harness/contracts/case_archive_knowledge_sync.json")


def _load_contract() -> dict:
    return json.loads(CONTRACT.read_text(encoding="utf-8"))


def test_case_archive_knowledge_sync_contract_is_valid() -> None:
    contract = _load_contract()

    assert contract["contract_id"] == "case_archive_knowledge_sync"
    assert contract["paths"]["archive_root"] == "data/case_archives"
    assert contract["paths"]["sync_records"] == "data/case_archives/sync_records.jsonl"
    assert contract["paths"]["plugin"] == "data/plugins/case_plugin/main.py"


def test_case_archive_knowledge_sync_has_unique_criteria_ids() -> None:
    criteria = _load_contract()["acceptance_criteria"]
    ids = [item["id"] for item in criteria]

    assert len(ids) == len(set(ids))


def test_case_archive_knowledge_sync_runtime_wiring() -> None:
    plugin_source = Path("data/plugins/case_plugin/main.py").read_text(encoding="utf-8")
    engine_source = Path("dc_engines/dc_engines/case/engine.py").read_text(
        encoding="utf-8"
    )
    sync_source = Path("dc_engines/dc_engines/case/knowledge_sync.py").read_text(
        encoding="utf-8"
    )

    assert "CaseKnowledgeSync" in engine_source
    assert "kb-sync stub" not in engine_source
    assert 'archive_root=data_dir / "case_archives"' in plugin_source
    assert "archive_to_nas" in plugin_source
    assert "sync-status" in plugin_source
    assert "latest_record_for_case" in plugin_source
    assert "sync_records.jsonl" in sync_source
    assert "latest_record_for_case" in sync_source
    assert "json.loads" in sync_source
    assert "json.JSONDecodeError" in sync_source


def test_case_archive_knowledge_sync_runtime_path_is_ignored() -> None:
    gitignore_source = Path(".gitignore").read_text(encoding="utf-8")

    assert "data/case_archives/" in gitignore_source
