from pathlib import Path

from harness.evaluator.kb_import_contract import (
    load_contract,
    validate_contract,
    verification_commands,
)

CONTRACT = Path("harness/contracts/unified_feishu_identity_connector.json")


def test_unified_feishu_identity_connector_contract_is_valid() -> None:
    contract = load_contract(CONTRACT)

    assert validate_contract(contract) == []


def test_unified_feishu_identity_connector_contract_covers_entrypoints() -> None:
    contract = load_contract(CONTRACT)

    architecture = contract["target_architecture"]
    assert architecture["identity_key"] == "tenant_key + union_id"
    assert architecture["oauth_purposes"] == [
        "employee_login",
        "desktop_login",
        "cloud_docs",
    ]
    assert contract["preserved_contracts"] == [
        "organization_permission_model",
        "desktop_employee_onboarding",
        "feishu_assistant_workbench",
    ]
    assert [item["id"] for item in contract["acceptance_criteria"]] == [
        "ufic-001",
        "ufic-002",
        "ufic-003",
        "ufic-004",
        "ufic-005",
        "ufic-006",
        "ufic-007",
    ]
    assert all(command.strip() for command in verification_commands(contract))
