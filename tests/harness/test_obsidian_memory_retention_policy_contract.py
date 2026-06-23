from pathlib import Path

from harness.evaluator.kb_import_contract import (
    load_contract,
    validate_contract,
    verification_commands,
)

CONTRACT = Path("harness/contracts/obsidian_memory_retention_policy.json")


def _contract() -> dict:
    return load_contract(CONTRACT)


def test_obsidian_memory_retention_policy_contract_is_valid() -> None:
    assert validate_contract(_contract()) == []


def test_retention_policy_is_propose_only_by_default() -> None:
    contract = _contract()
    text = "\n".join(
        [
            contract["goal"],
            *contract["non_goals"],
            *[
                criterion["description"]
                for criterion in contract["acceptance_criteria"]
            ],
        ]
    )

    assert "propose-only" in text
    assert "human approval" in text
    assert "no automatic deletion" in text
    assert "raw source" in text


def test_retention_policy_points_to_safety_verifiers() -> None:
    commands = "\n".join(verification_commands(_contract()))

    assert "test_obsidian_memory_retention_policy_contract.py" in commands
    assert "test_recall_filters_to_approved_by_default" in commands
