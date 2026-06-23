from pathlib import Path

from harness.evaluator.kb_import_contract import (
    load_contract,
    validate_contract,
    verification_commands,
)

CONTRACT = Path("harness/contracts/organization_permission_model.json")


def test_organization_permission_model_contract_is_valid() -> None:
    contract = load_contract(CONTRACT)

    assert validate_contract(contract) == []


def test_organization_permission_model_contract_keeps_identity_layers_separate() -> None:
    contract = load_contract(CONTRACT)

    assert contract["target_architecture"]["identity_layers"] == [
        "organization_identity",
        "external_platform_identity",
        "dc_runtime_permission",
    ]
    assert (
        contract["target_architecture"]["permission_runtime"]
        == "dc_engines.org_permissions"
    )


def test_organization_permission_model_contract_points_to_verifiers() -> None:
    contract = load_contract(CONTRACT)

    assert verification_commands(contract) == [
        "uv run pytest dc_engines/tests/test_org_permissions.py::test_canonical_department_tree_and_aliases -q",
        "uv run pytest dc_engines/tests/test_org_permissions.py::test_feishu_admin_fact_does_not_grant_dc_admin -q",
        "uv run pytest dc_engines/tests/test_org_permissions.py::test_manager_and_boss_scoped_permissions -q",
        "uv run pytest dc_engines/tests/test_org_permissions.py::test_admins_id_and_app_principal_separation -q",
    ]
