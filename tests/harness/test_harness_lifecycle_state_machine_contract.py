from pathlib import Path

from harness.evaluator.kb_import_contract import (
    load_contract,
    validate_contract,
    verification_commands,
)

CONTRACT = Path("harness/contracts/harness_lifecycle_state_machine.json")


def test_harness_lifecycle_state_machine_contract_is_valid() -> None:
    contract = load_contract(CONTRACT)

    assert validate_contract(contract) == []


def test_harness_lifecycle_state_machine_contract_has_unique_criteria_ids() -> None:
    contract = load_contract(CONTRACT)
    criterion_ids = [criterion["id"] for criterion in contract["acceptance_criteria"]]

    assert len(criterion_ids) == len(set(criterion_ids))


def test_harness_lifecycle_state_machine_contract_points_to_verifiers() -> None:
    contract = load_contract(CONTRACT)

    assert verification_commands(contract) == [
        "uv run pytest dc_engines/tests/test_harness_lifecycle.py::test_state_machine_rejects_terminal_task_reopen dc_engines/tests/test_harness_lifecycle.py::test_state_machine_rejects_blocked_direct_completion dc_engines/tests/test_harness_lifecycle.py::test_state_machine_store_rejects_stale_status_update -q",
        "uv run pytest dc_engines/tests/test_harness_lifecycle.py::test_review_required_completion_requires_approved_review dc_engines/tests/test_harness_lifecycle.py::test_review_required_by_default_cannot_complete_before_review_gate dc_engines/tests/test_harness_lifecycle.py::test_approve_task_requires_review_required_status -q",
        "uv run pytest dc_engines/tests/test_harness_sensor_plugin.py::test_sensor_excludes_review_required_from_auto_complete dc_engines/tests/test_harness_sensor_plugin.py::test_sensor_settle_skips_review_required_success_response dc_engines/tests/test_harness_sensor_plugin.py::test_sensor_excludes_review_required_by_default_auto_complete dc_engines/tests/test_harness_sensor_plugin.py::test_sensor_keeps_success_text_with_negated_missing_terms_success dc_engines/tests/harness_sensor_plugin_truth_intake_test.py::test_sensor_classifies_missing_materials_as_insufficient dc_engines/tests/harness_sensor_plugin_truth_intake_test.py::test_sensor_classifies_no_hits_and_whitelist_missing_as_insufficient -q",
        "uv run pytest dc_engines/tests/test_workflow_intent_plugin.py::test_workflow_intent_review_required_kind_disables_auto_complete dc_engines/tests/test_workflow_intent_plugin.py::test_workflow_intent_project_followup_auto_completes_when_allowed dc_engines/tests/test_department_workflows.py::test_build_request_payload_contains_harness_requirements dc_engines/tests/test_department_workflows.py::test_department_requester_meta_cannot_override_auto_complete_gate dc_engines/tests/test_department_workflows.py::test_client_followup_material_gap_blocks_generation dc_engines/tests/test_department_workflows.py::test_client_private_domain_ready_payload_has_review_outputs -q",
        "uv run pytest tests/test_department_workflow_plugin_content_sop.py::test_department_workflow_plugin_does_not_override_auto_complete_gate -q",
        "uv run pytest dc_engines/tests/test_harness_runtime_hooks.py::test_sensor_classifies_media_ack_as_acknowledged dc_engines/tests/test_harness_sensor_plugin.py::test_sensor_does_not_complete_acknowledged_response -q",
    ]
