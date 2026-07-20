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
    assert validate_contract(_contract()) == []


def test_obsidian_vault_automation_contract_has_unique_criteria_ids() -> None:
    contract = _contract()
    criterion_ids = [criterion["id"] for criterion in contract["acceptance_criteria"]]

    assert len(criterion_ids) == len(set(criterion_ids))


def test_contract_points_to_wrapper_and_contract_verifiers() -> None:
    commands = verification_commands(_contract())

    assert commands == [
        "uv run pytest dc_engines/tests/test_obsidian_vault_wrapper.py::test_rejects_path_traversal_and_symlink_escape -q",
        "uv run pytest dc_engines/tests/test_obsidian_vault_wrapper.py::test_read_only_wrapper_lists_reads_searches_frontmatter_and_backlinks -q",
        "uv run pytest dc_engines/tests/test_obsidian_vault_wrapper.py::test_write_requests_are_dry_run_audited_and_do_not_mutate_vault -q",
        "uv run pytest tests/harness/test_obsidian_vault_automation_contract.py::test_contract_records_agent_skill_execution_boundaries -q",
        "uv run pytest tests/harness/test_obsidian_vault_automation_contract.py::test_contract_records_phase1_scope_and_non_goals -q",
        "uv run pytest tests/test_obsidian_vault_tools.py -q",
    ]


def test_contract_records_permission_model() -> None:
    model = _contract()["permission_model"]

    assert model["default_mode"] == "read_only"
    assert "Configured paths only" in model["vault_roots"]
    assert "allowlisted" in model["write_policy"]
    assert "dry-run" in model["write_policy"]
    assert "audit" in model["write_policy"]

    forbidden = "\n".join(model["forbidden_operations"])
    assert "arbitrary shell execution" in forbidden
    assert "arbitrary absolute paths" in forbidden
    assert "path traversal" in forbidden
    assert "bulk delete" in forbidden
    assert "direct obsidian-cli" in forbidden

    for field in ("actor", "action", "target_path", "dry_run", "allowed"):
        assert field in model["audit"]


def test_contract_records_agent_skill_execution_boundaries() -> None:
    boundaries = _contract()["agent_skill_boundaries"]

    assert "controlled wrapper" in boundaries["obsidian_cli"]
    assert "must not execute obsidian-cli directly" in boundaries["obsidian_cli"]
    assert "isolated staging queue" in boundaries["capture_cleaning"]
    assert "cannot write the vault directly" in boundaries["capture_cleaning"]
    assert "authoring_skill" in boundaries["skill_types"]
    assert "execution_skill" in boundaries["skill_types"]
    assert "dry-run plans" in boundaries["skill_types"]["execution_skill"]


def test_contract_records_phase1_scope_and_non_goals() -> None:
    contract = _contract()

    assert contract["phase1_interfaces"]["read"] == [
        "list_directory",
        "read_note",
        "search_notes",
        "parse_frontmatter",
        "read_backlinks",
    ]
    assert contract["phase1_interfaces"]["write_dry_run_only"] == ["create_note"]
    assert "sync_company_knowledge_map" in contract["phase1_interfaces"]["future_write"]
    assert "sync_rawrefs" in contract["phase1_interfaces"]["future_write"]
    assert "sync_review_workbench" in contract["phase1_interfaces"]["future_write"]

    non_goals = "\n".join(contract["non_goals"])
    assert "live write-back" in non_goals
    assert "obsidian-cli directly" in non_goals
    assert "bulk delete" in non_goals
    assert "bulk move" in non_goals


def test_contract_records_live_agent_and_nas_read_only_integration() -> None:
    integration = _contract()["runtime_integration"]

    assert integration["vault_mount"] == "/AstrBot/ObsidianVault"
    assert integration["mount_mode"] == "read_only"
    assert integration["authorization"] == "AstrBot admins_id"
    assert integration["llm_tools"] == [
        "search_obsidian_vault",
        "read_obsidian_note",
        "list_obsidian_vault",
    ]
    assert integration["context_policy"] == "retrieve_on_demand"
