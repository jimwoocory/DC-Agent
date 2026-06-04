from pathlib import Path

from harness.evaluator.kb_import_contract import (
    load_contract,
    validate_contract,
    verification_commands,
)

CONTRACTS = (
    Path("harness/contracts/client_content_sop.json"),
    Path("harness/contracts/planning_content_sop.json"),
    Path("harness/contracts/media_generation_sop.json"),
    Path("harness/contracts/content_sop_system.json"),
)


def test_content_sop_contracts_are_valid() -> None:
    for path in CONTRACTS:
        assert validate_contract(load_contract(path)) == []


def test_content_sop_contracts_have_unique_criteria_ids() -> None:
    for path in CONTRACTS:
        contract = load_contract(path)
        criterion_ids = [
            criterion["id"] for criterion in contract["acceptance_criteria"]
        ]

        assert len(criterion_ids) == len(set(criterion_ids))


def test_content_sop_contracts_point_to_runtime_verifiers() -> None:
    commands = {
        command
        for path in CONTRACTS
        for command in verification_commands(load_contract(path))
    }

    assert any("test_router_core.py" in command for command in commands)
    assert any("test_department_workflows.py" in command for command in commands)
    assert any("test_harness_lifecycle.py" in command for command in commands)
    assert any("test_feishu_card_templates.py" in command for command in commands)
    assert any("test_content_sop_quality_gate.py" in command for command in commands)


def test_content_sop_docs_cover_operator_flow() -> None:
    content = Path("docs/CONTENT_SOP.md").read_text(encoding="utf-8")

    for heading in (
        "Start A Request",
        "Supplement Materials",
        "Confirm Delivery",
        "Roll Back",
    ):
        assert heading in content
