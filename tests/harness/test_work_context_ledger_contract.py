from pathlib import Path

from harness.evaluator.kb_import_contract import (
    load_contract,
    validate_contract,
    verification_commands,
)

CONTRACT = Path("harness/contracts/work_context_ledger.json")


def test_work_context_ledger_contract_is_valid() -> None:
    contract = load_contract(CONTRACT)

    assert validate_contract(contract) == []


def test_work_context_ledger_contract_has_unique_criteria_ids() -> None:
    contract = load_contract(CONTRACT)
    criterion_ids = [criterion["id"] for criterion in contract["acceptance_criteria"]]

    assert len(criterion_ids) == len(set(criterion_ids))


def test_work_context_ledger_contract_points_to_required_verifiers() -> None:
    commands = verification_commands(load_contract(CONTRACT))

    assert any("test_work_context_ledger.py" in command for command in commands)
    assert any("test_media_work_context_ledger.py" in command for command in commands)


def test_work_context_ledger_contract_preserves_module_boundaries() -> None:
    contract = load_contract(CONTRACT)

    assert contract["runtime_model"]["owner"] == ("dc_engines.harness.HarnessTaskStore")
    assert contract["runtime_model"]["terminal_task_policy"] == (
        "create_revision_child_task"
    )
    assert contract["scope_key_policy"]["forbidden_as_sole_identity"] == [
        "unified_msg_origin",
        "process_memory_cache",
    ]
    assert "Do not make Harness the Router or scheduler." in contract["non_goals"]
