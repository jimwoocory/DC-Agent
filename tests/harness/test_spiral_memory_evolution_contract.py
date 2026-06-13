from pathlib import Path

from harness.evaluator.kb_import_contract import (
    load_contract,
    validate_contract,
    verification_commands,
)

CONTRACT = Path("harness/contracts/spiral_memory_evolution.json")


def test_spiral_memory_evolution_contract_is_valid() -> None:
    contract = load_contract(CONTRACT)

    assert validate_contract(contract) == []


def test_spiral_memory_evolution_contract_points_to_required_verifiers() -> None:
    contract = load_contract(CONTRACT)

    assert verification_commands(contract) == [
        "uv run pytest dc_engines/tests/test_spiral_evolution.py::test_content_sop_payload_carries_spiral_seed -q",
        "uv run pytest dc_engines/tests/test_spiral_evolution.py::test_spiral_snapshot_creates_need_review_memory_candidate -q",
        "uv run pytest dc_engines/tests/test_spiral_evolution.py::test_detect_stable_rule_candidates_requires_approved_support -q",
        "uv run pytest dc_engines/tests/test_spiral_evolution.py::test_subagent_driven_upgrade_plan_requires_reviews_and_gates -q",
        "uv run pytest dc_engines/tests/test_harness_lifecycle.py::test_content_sop_result_settlement_attaches_spiral_snapshot -q",
        "uv run pytest dc_engines/tests/test_content_sop_rule_overrides.py::test_apply_rule_proposal_requires_runtime_approval dc_engines/tests/test_content_sop_rule_overrides.py::test_apply_rule_proposal_rejects_generic_memory_approval -q",
        "uv run pytest dc_engines/tests/test_content_sop_rule_overrides.py::test_apply_rule_proposal_writes_versioned_override dc_engines/tests/test_content_sop_rule_overrides.py::test_apply_rule_proposal_is_idempotent -q",
        "uv run pytest dc_engines/tests/test_content_sop_rule_overrides.py::test_content_sop_payload_hot_reads_matching_rule -q",
        "uv run pytest dc_engines/tests/test_content_sop_rule_overrides.py::test_rollback_rule_override_disables_runtime_rule -q",
        "uv run pytest dc_engines/tests/test_content_sop_rule_overrides.py::test_rule_override_load_fails_closed_on_malformed_json dc_engines/tests/test_content_sop_rule_overrides.py::test_rule_override_load_fails_closed_on_invalid_schema dc_engines/tests/test_content_sop_rule_overrides.py::test_rule_override_load_fails_closed_on_invalid_rule_entry -q",
        "uv run pytest dc_engines/tests/test_content_sop_rule_overrides.py::test_rule_override_save_keeps_rollback_backup -q",
        "uv run pytest dc_engines/tests/test_content_sop_rule_proposals.py::test_approve_apply_and_rollback_rule_proposal -q",
        "uv run pytest dc_engines/tests/test_content_sop_rule_review_plugin.py::test_content_sop_rule_plugin_rejects_untrusted_card_action dc_engines/tests/test_content_sop_rule_review_plugin.py::test_content_sop_rule_plugin_requires_admin_reviewer -q",
        "uv run pytest dc_engines/tests/test_content_sop_rule_review_plugin.py::test_content_sop_rule_plugin_approve_then_confirm_apply dc_engines/tests/test_content_sop_rule_review_plugin.py::test_content_sop_rule_plugin_rollback_confirm -q",
        "uv run pytest dc_engines/tests/test_obsidian_memory_export_import.py::test_import_approved_content_sop_memories_drafts_rule_proposal dc_engines/tests/test_spiral_evolution.py::test_draft_rule_proposals_from_approved_governed_memory_store -q",
        "uv run pytest dc_engines/tests/test_harness_lifecycle.py::test_content_sop_result_settlement_exports_memory_candidate_to_obsidian dc_engines/tests/test_obsidian_memory_export_import.py::test_exported_content_sop_need_review_memory_is_not_default_recall -q",
        "uv run pytest dc_engines/tests/test_content_sop_ops.py::test_ops_dashboard_summarizes_full_content_sop_chain -q",
        "uv run pytest dc_engines/tests/test_content_sop_ops.py::test_ops_reminders_and_card_cover_feishu_admin_actions dc_engines/tests/test_content_sop_rule_review_plugin.py::test_content_sop_rule_plugin_sends_ops_reminder_card -q",
        "uv run pytest dc_engines/tests/test_content_sop_ops.py::test_scheduled_ops_exports_audit_report dc_engines/tests/test_content_sop_ops.py::test_audit_report_export_is_runtime_function_not_test_only_script -q",
        "uv run pytest tests/test_content_sop_ops_route.py::test_content_sop_ops_dashboard_route_reads_runtime_store tests/test_content_sop_ops_route.py::test_content_sop_ops_scheduled_route_exports_report -q",
        "uv run pytest dc_engines/tests/test_content_sop_ops.py::test_production_config_confirmation_requires_real_ops_switches -q",
    ]


def test_spiral_memory_evolution_contract_separates_automatic_and_human_gated() -> None:
    contract = load_contract(CONTRACT)
    policy = contract["autonomy_policy"]

    assert "create_memory_candidate" in policy["automatic_allowed"]
    assert "detect_stable_pattern" in policy["automatic_allowed"]
    assert "apply_sop_upgrade_to_runtime" in policy["human_gated"]
    assert "change_router_or_harness_rules" in policy["human_gated"]
    assert "Memory can grow automatically" in policy["hard_rule"]


def test_spiral_memory_evolution_contract_requires_subagent_review_chain() -> None:
    contract = load_contract(CONTRACT)
    gate = contract["target_architecture"]["subagent_driven_gate"]

    assert gate == [
        "implementer",
        "spec_compliance_reviewer",
        "code_quality_reviewer",
        "verification_runner",
    ]
    assert (
        contract["target_architecture"]["first_apply_target"]
        == "data/config/content_sop_rule_overrides.json hot-read by content SOP payload"
    )
    assert (
        contract["target_architecture"]["need_review_memory_export_entrypoint"]
        == "dc_engines.harness.content_sop_runtime.settle_content_sop_result"
    )
    assert (
        contract["target_architecture"]["human_review_entrypoint"]
        == "data/plugins/content_sop_rule_review_plugin"
    )
    assert (
        contract["target_architecture"]["approved_memory_to_rule_proposal_entrypoint"]
        == "dc_engines.memory_governance.importer.import_governance_notes"
    )
    assert (
        "Do not promote need_review memory into runtime recall."
        in contract["non_goals"]
    )
