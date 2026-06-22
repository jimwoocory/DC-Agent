from pathlib import Path

from harness.evaluator.loop_engineering_mvp import load_contract, validate_contract

CONTRACT = Path("harness/contracts/loop_engineering_mvp.json")


def test_loop_engineering_mvp_contract_is_valid() -> None:
    contract = load_contract(CONTRACT)

    assert validate_contract(contract) == []


def test_loop_engineering_mvp_contract_has_unique_criteria_ids() -> None:
    contract = load_contract(CONTRACT)
    criterion_ids = [criterion["id"] for criterion in contract["acceptance_criteria"]]

    assert len(criterion_ids) == len(set(criterion_ids))


def test_loop_engineering_mvp_contract_is_targeted_check_referenced() -> None:
    script = Path("scripts/agent-check.sh").read_text(encoding="utf-8")

    assert "harness.evaluator.loop_engineering_mvp" in script
    assert "tests/harness/test_loop_engineering_mvp_contract.py" in script
    assert "tests/test_harness_loop_route.py" in script
