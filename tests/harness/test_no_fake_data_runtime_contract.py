from pathlib import Path

from harness.evaluator.kb_import_contract import (
    load_contract,
    validate_contract,
    verification_commands,
)

CONTRACT = Path("harness/contracts/no_fake_data_runtime.json")


def test_no_fake_data_runtime_contract_is_valid() -> None:
    contract = load_contract(CONTRACT)

    assert validate_contract(contract) == []


def test_no_fake_data_runtime_contract_has_unique_criteria_ids() -> None:
    contract = load_contract(CONTRACT)
    criterion_ids = [criterion["id"] for criterion in contract["acceptance_criteria"]]

    assert len(criterion_ids) == len(set(criterion_ids))


def test_no_fake_data_runtime_contract_points_to_guard_verifiers() -> None:
    contract = load_contract(CONTRACT)

    assert verification_commands(contract) == [
        "uv run python scripts/check_fake_data_guard.py",
        "uv run pytest tests/test_fake_data_guard.py -q",
        "uv run pytest tests/harness/test_no_fake_data_runtime_contract.py -q",
        "uv run pytest dc_engines/tests/test_harness_lifecycle.py -q",
        "uv run pytest tests/test_feishu_pet_assistant.py -q",
        "uv run pytest tests/harness/test_hermes_bridge.py -q",
        "uv run pytest dc_engines/tests/test_task_cli_plugin.py -q",
    ]


def test_no_fake_data_runtime_contract_documents_manual_commands() -> None:
    contract = load_contract(CONTRACT)
    manual = contract["verification"]["manual"]

    assert "uv run pytest dc_engines/tests/test_harness_lifecycle.py -q" in manual
    assert (
        "uv run pytest tests/test_fake_data_guard.py "
        "tests/harness/test_no_fake_data_runtime_contract.py "
        "tests/test_feishu_pet_assistant.py "
        "tests/harness/test_hermes_bridge.py -q"
    ) in manual
    assert "uv run pytest dc_engines/tests/test_task_cli_plugin.py -q" in manual
    assert "uv run python scripts/check_fake_data_guard.py" in manual
    assert any(command.startswith("uv run ruff check ") for command in manual)
    assert any(command.startswith("uv run ruff format --check ") for command in manual)
