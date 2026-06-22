from pathlib import Path

from harness.evaluator.kb_import_contract import (
    load_contract,
    validate_contract,
    verification_commands,
)

CONTRACT = Path("harness/contracts/obsidian_vault_automation.json")


def _contract() -> dict:
    return load_contract(CONTRACT)


def test_obsidian_vault_automation_contract_is_valid() -> None:
    contract = _contract()

    assert validate_contract(contract) == []


def test_obsidian_vault_automation_contract_has_unique_criteria_ids() -> None:
    contract = _contract()
    criterion_ids = [criterion["id"] for criterion in contract["acceptance_criteria"]]

    assert len(criterion_ids) == len(set(criterion_ids))


def test_obsidian_vault_automation_contract_points_to_phase_one_verifiers() -> None:
    contract = _contract()

    assert verification_commands(contract) == [
        "uv run pytest tests/harness/test_obsidian_vault_automation_contract.py::test_obsidian_vault_automation_contract_is_valid -q",
        "uv run pytest dc_engines/tests/test_obsidian_vault_automation.py::test_wrapper_enforces_vault_root_allowlist -q",
        "uv run pytest dc_engines/tests/test_obsidian_vault_automation.py::test_wrapper_blocks_traversal_and_symlink_escape -q",
        "uv run pytest dc_engines/tests/test_obsidian_vault_automation.py::test_wrapper_supports_read_only_vault_operations -q",
        "uv run pytest dc_engines/tests/test_obsidian_vault_automation.py::test_wrapper_denies_write_delete_and_shell_execution -q",
        "uv run pytest dc_engines/tests/test_obsidian_vault_automation.py::test_wrapper_writes_append_only_audit_records -q",
    ]


def test_contract_records_read_only_operations_and_denied_execution() -> None:
    contract = _contract()

    assert contract["phase"] == "phase_1_read_only"
    assert set(contract["allowed_operations"]) == {
        "list",
        "read",
        "search",
        "metadata",
        "frontmatter",
        "plan_write",
    }
    assert {"write", "delete", "shell", "obsidian_cli", "defuddle"} <= set(
        contract["denied_operations"]
    )


def test_contract_requires_vault_allowlist_and_append_only_audit() -> None:
    contract = _contract()
    boundaries = contract["security_boundaries"]

    assert boundaries["vault_root_allowlist"] is True
    assert boundaries["pathlib_path_resolution"] is True
    assert boundaries["symlink_escape_blocking"] is True
    assert boundaries["arbitrary_shell"] == "forbidden"
    assert boundaries["default_write_mode"] == "dry_run_only"
    assert boundaries["audit_log"] == "append_only_jsonl"


def test_contract_defers_obsidian_cli_and_defuddle() -> None:
    contract = _contract()
    strategy = contract["execution_skill_strategy"]
    non_goals = "\n".join(contract["non_goals"])

    assert (
        strategy["obsidian_cli"] == "deferred_until_controlled_wrapper_adapter_exists"
    )
    assert strategy["defuddle"] == "deferred_until_controlled_wrapper_adapter_exists"
    assert "raw obsidian-cli" in non_goals
    assert "defuddle" in non_goals
