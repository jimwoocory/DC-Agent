from pathlib import Path

from harness.evaluator.kb_import_contract import (
    load_contract,
    validate_contract,
    verification_commands,
)

CONTRACT = Path("harness/contracts/hermes_deep_runtime_registry.json")


def test_hermes_deep_runtime_registry_contract_is_valid() -> None:
    contract = load_contract(CONTRACT)

    assert validate_contract(contract) == []


def test_hermes_deep_runtime_registry_contract_has_unique_criteria_ids() -> None:
    contract = load_contract(CONTRACT)
    criterion_ids = [criterion["id"] for criterion in contract["acceptance_criteria"]]

    assert len(criterion_ids) == len(set(criterion_ids))


def test_hermes_deep_runtime_registry_contract_covers_registry_and_bridge() -> None:
    contract = load_contract(CONTRACT)
    criteria_by_id = {
        criterion["id"]: criterion for criterion in contract["acceptance_criteria"]
    }

    assert "RuntimeRegistry" in criteria_by_id["hermes-runtime-001"]["description"]
    assert "dashboard payload" in criteria_by_id["hermes-runtime-001"]["description"]
    assert "adapter_wired" in criteria_by_id["hermes-runtime-001"]["description"]
    assert "HermesBridge" in criteria_by_id["hermes-runtime-002"]["description"]
    assert "adapter_not_wired" in criteria_by_id["hermes-runtime-002"]["description"]


def test_hermes_deep_runtime_registry_contract_points_to_verifiers() -> None:
    contract = load_contract(CONTRACT)

    assert verification_commands(contract) == [
        "uv run pytest tests/test_deep_runtime_closure.py -q",
        "uv run pytest tests/harness/test_hermes_bridge.py -q",
    ]
