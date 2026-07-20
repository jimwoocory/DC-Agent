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


def test_organization_permission_model_contract_keeps_identity_layers_separate() -> (
    None
):
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
        "uv run python scripts/run_cross_suite_pytests.py --dc-engine dc_engines/tests/test_employee_directory.py::test_requester_meta_from_event_resolves_admin_without_employee_profile --root tests/harness/test_organization_permission_model_contract.py::test_runtime_principal_adapter_precedes_router",
        "uv run pytest dc_engines/tests/test_employee_directory.py::test_requester_meta_from_event_resolves_persisted_permission_without_employee_profile -q",
        "uv run pytest dc_engines/tests/test_org_permissions.py::test_authorization_decision_enforces_subject_type_enabled_and_scope -q",
        "uv run pytest dc_engines/tests/test_content_sop_rule_review_plugin.py::test_content_sop_rule_plugin_rejects_permission_bound_to_another_subject dc_engines/tests/test_concierge_identity.py::test_employee_debug_commands_reject_permission_bound_to_another_subject -q",
        "uv run python scripts/run_cross_suite_pytests.py --dc-engine dc_engines/tests/test_workflow_intent_plugin.py::test_workflow_intent_task_contains_runtime_principal --root tests/test_department_workflow_plugin_content_sop.py::test_department_workflow_task_contains_runtime_principal",
        "uv run pytest dc_engines/tests/test_employee_directory_sync.py::test_feishu_sync_preview_is_read_only_and_apply_requires_matching_plan -q",
        "uv run pytest dc_engines/tests/test_employee_directory_sync.py::test_non_authoritative_sync_preserves_existing_values_and_reports_conflicts -q",
        "uv run python scripts/run_cross_suite_pytests.py --dc-engine dc_engines/tests/test_concierge_identity.py::test_employee_directory_auto_sync_defaults_to_safe_preview --root tests/harness/test_organization_permission_model_contract.py::test_employee_sync_command_requires_control_plan",
        "uv run pytest dc_engines/tests/test_employee_directory_sync.py::test_feishu_sync_uses_roster_role_only_after_three_way_identity_match -q",
    ]


def test_runtime_principal_adapter_precedes_router() -> None:
    contract = load_contract(CONTRACT)
    architecture = contract["target_architecture"]

    assert architecture["event_priority_order"] == [
        "feishu_channel_control:200",
        "runtime_principal:190",
        "dc_router:15",
    ]
    assert architecture["runtime_principal_event_key"] == "dc_runtime_principal"
    concierge_source = Path("data/plugins/concierge_plugin/main.py").read_text(
        encoding="utf-8"
    )
    assert "async def resolve_runtime_principal" in concierge_source
    assert "priority=190" in concierge_source
    channel_source = Path("data/plugins/feishu_channel_control/main.py").read_text(
        encoding="utf-8"
    )
    assert "priority=200" in channel_source


def test_employee_sync_command_requires_control_plan() -> None:
    source = Path("data/plugins/concierge_plugin/main.py").read_text(encoding="utf-8")

    assert "/employees sync apply <plan_id>" in source
    assert "preview_only=not apply_requested" in source
    assert "expected_plan_id=expected_plan_id" in source
