import json
from pathlib import Path

from harness.evaluator.kb_import_contract import (
    load_contract,
    validate_contract,
    verification_commands,
)

CONTRACT = Path("harness/contracts/employee_insight_loop.json")


def test_employee_insight_loop_contract_is_valid() -> None:
    contract = load_contract(CONTRACT)

    assert validate_contract(contract) == []
    assert contract["contract_id"] == "employee_insight_loop"


def test_employee_insight_loop_contract_has_unique_criteria_ids() -> None:
    contract = load_contract(CONTRACT)
    criterion_ids = [criterion["id"] for criterion in contract["acceptance_criteria"]]

    assert len(criterion_ids) == len(set(criterion_ids))


def test_employee_insight_loop_contract_points_to_required_verifiers() -> None:
    contract = load_contract(CONTRACT)

    assert list(dict.fromkeys(verification_commands(contract))) == [
        "uv run pytest tests/harness/test_employee_insight_loop_contract.py -q",
        "uv run pytest dc_engines/tests/test_employee_insight_loop.py -q",
    ]


def test_employee_insight_loop_contract_requires_governed_closed_loop() -> None:
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    text = json.dumps(contract, ensure_ascii=False)

    for required in [
        "lark_dm",
        "outreach_frequency_limit",
        "opt_out",
        "employee_insight_events",
        "review_required",
        "need_review",
        "Obsidian",
        "Hermes",
        "approved-only",
        "must not modify router",
        "must not auto-publish skill",
        "append-only audit",
        "dashboard",
    ]:
        assert required in text


def test_employee_insight_loop_contract_documents_runtime_surfaces() -> None:
    contract = load_contract(CONTRACT)
    runtime = contract["runtime_surfaces"]

    assert runtime["employee_channel"] == "lark_dm"
    assert runtime["management_surface"] == "dashboard"
    assert (
        runtime["governance_surface"]
        == "ObsidianVault/40_MemoryGovernance/EmployeeInsight"
    )
    assert "AstrBot" in runtime["execution_surfaces"]
    assert "router" in runtime["execution_surfaces"]
    assert "Hermes" in runtime["execution_surfaces"]
