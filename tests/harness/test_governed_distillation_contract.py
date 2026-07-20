from __future__ import annotations

import json
from pathlib import Path


def test_governed_distillation_contract_is_complete() -> None:
    contract = json.loads(
        Path("harness/contracts/governed_distillation_loop.json").read_text(
            encoding="utf-8"
        )
    )

    criteria = contract["acceptance_criteria"]
    assert {item["id"] for item in criteria} == {
        "gdl-001",
        "gdl-002",
        "gdl-003",
        "gdl-004",
        "gdl-005",
        "gdl-006",
    }
    assert contract["runtime_model"]["event_source"].endswith("HarnessEngine")
    assert contract["runtime_model"]["approval_adapter"].startswith("ObsidianVault/")
    assert all(item.get("verification") for item in criteria)


def test_governed_distillation_contract_forbids_full_result_persistence() -> None:
    contract = json.loads(
        Path("harness/contracts/governed_distillation_loop.json").read_text(
            encoding="utf-8"
        )
    )
    adapter_source = Path(
        "dc_engines/dc_engines/memory_governance/task_distillation.py"
    ).read_text(encoding="utf-8")
    exporter_source = Path(
        "dc_engines/dc_engines/memory_governance/exporter.py"
    ).read_text(encoding="utf-8")

    assert any("complete prompts" in item for item in contract["non_goals"])
    assert "record.summary" in exporter_source
    assert "record.payload" not in adapter_source + exporter_source
