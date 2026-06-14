import json
from pathlib import Path

CONTRACT = Path("harness/contracts/lark_archive_distillation_handoff.json")


def _load_contract() -> dict:
    return json.loads(CONTRACT.read_text(encoding="utf-8"))


def test_lark_archive_distillation_handoff_contract_is_valid() -> None:
    contract = _load_contract()

    assert contract["contract_id"] == "lark_archive_distillation_handoff"
    assert contract["paths"]["nas_archive_root"] == "knowledge/Chat/Lark Chat"
    assert contract["paths"]["obsidian_inbox"].endswith("40_MemoryGovernance/Inbox")


def test_lark_archive_distillation_handoff_has_unique_criteria_ids() -> None:
    criteria = _load_contract()["acceptance_criteria"]
    ids = [item["id"] for item in criteria]

    assert len(ids) == len(set(ids))


def test_lark_archive_distillation_handoff_points_to_runtime_worker() -> None:
    worker_source = Path("scripts-watchdog/assistant_distillation_worker.py").read_text(
        encoding="utf-8"
    )
    module_source = Path(
        "dc_engines/dc_engines/lark_archive_distillation.py"
    ).read_text(encoding="utf-8")

    assert "run_lark_archive_handoff" in worker_source
    assert "--lark-archive-root" in worker_source
    assert "export_lark_archive_handoff" in module_source
    assert "AssistantDistillationStore" in module_source
