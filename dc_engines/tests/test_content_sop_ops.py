from __future__ import annotations

import json
from pathlib import Path

from dc_engines.department_workflows.content_rule_proposals import (
    ContentSopRuleProposalStore,
    approve_rule_proposal,
)
from dc_engines.department_workflows.content_sop_ops import (
    build_content_sop_ops_dashboard,
    build_content_sop_ops_reminder_card,
    build_content_sop_ops_reminders,
    confirm_content_sop_production_config,
    export_content_sop_ops_audit_report,
    run_scheduled_content_sop_ops,
)
from dc_engines.memory_governance.models import GovernedMemory
from dc_engines.memory_governance.store import MemoryGovernanceStore


def _proposal(proposal_id: str = "proposal_ops") -> dict:
    return {
        "proposal_id": proposal_id,
        "department_id": "client_dept",
        "scenario_id": "customer_greeting",
        "rule_type": "process",
        "rule_text": "客户触达默认使用飞书和私域，不默认使用邮件。",
        "support_count": 3,
        "evidence_candidate_ids": ["cand_1", "cand_2", "cand_3"],
        "status": "pending",
    }


def _memory(memory_id: str, status: str) -> GovernedMemory:
    return GovernedMemory(
        memory_id=memory_id,
        source_system="harness",
        source_id=f"task_{memory_id}",
        source_path="harness/tasks",
        source_hash=f"hash_{memory_id}",
        title="客户触达复盘",
        summary="员工确认内容 SOP 输出需要私域口径。",
        canonical_text="客户触达默认使用飞书和私域。",
        memory_kind="process",
        review_status=status,  # type: ignore[arg-type]
        confidence=0.9,
        sensitivity="internal",
        tags=["content_sop"],
        created_at="2026-06-05T00:00:00Z",
        updated_at="2026-06-05T00:00:00Z",
    )


def test_ops_dashboard_summarizes_full_content_sop_chain(tmp_path: Path) -> None:
    store = ContentSopRuleProposalStore(tmp_path / "proposals.db")
    store.upsert_proposal(_proposal("proposal_pending"))
    approved = store.upsert_proposal(_proposal("proposal_approved"))
    approve_rule_proposal(store, approved.proposal_id, reviewer="ou_admin")
    memory_store = MemoryGovernanceStore(tmp_path / "governed_memory.db")
    memory_store.initialize()
    memory_store.upsert_memory(_memory("mem_need_review", "need_review"))

    dashboard = build_content_sop_ops_dashboard(
        proposal_store=store,
        overrides_path=tmp_path / "content_sop_rule_overrides.json",
        governed_memory_db_path=tmp_path / "governed_memory.db",
        quality_results=[
            {"status": "blocked", "score": 52},
            {"status": "review_required", "score": 86},
        ],
        now="2026-06-05T00:00:00Z",
    )

    assert dashboard["proposals"]["by_status"] == {
        "pending": 1,
        "approved_for_runtime": 1,
    }
    assert dashboard["memory_governance"]["by_status"] == {"need_review": 1}
    assert dashboard["runtime_overrides"]["config_ok"] is False
    assert dashboard["runtime_overrides"]["status"] == "missing"
    assert dashboard["quality"]["average_score"] == 69
    assert dashboard["ops_sop"]["escalation"]


def test_ops_dashboard_reports_active_runtime_overrides_only_from_real_config(
    tmp_path: Path,
) -> None:
    store = ContentSopRuleProposalStore(tmp_path / "proposals.db")
    overrides_path = tmp_path / "content_sop_rule_overrides.json"
    overrides_path.write_text(
        json.dumps(
            {
                "version": 2,
                "rules": [
                    {
                        "rule_id": "content_sop_rule_proposal_active",
                        "proposal_id": "proposal_active",
                        "department_id": "client_dept",
                        "scenario_id": "customer_greeting",
                        "rule_type": "process",
                        "rule_text": "客户触达默认使用私域渠道。",
                        "source_candidate_ids": ["cand_1"],
                        "support_count": 3,
                        "enabled": True,
                    }
                ],
                "audit": [{"action": "apply_rule_proposal"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    dashboard = build_content_sop_ops_dashboard(
        proposal_store=store,
        overrides_path=overrides_path,
        governed_memory_db_path=tmp_path / "missing.db",
    )

    assert dashboard["runtime_overrides"]["config_ok"] is True
    assert dashboard["runtime_overrides"]["status"] == "active"
    assert dashboard["runtime_overrides"]["version"] == 2
    assert dashboard["runtime_overrides"]["enabled_count"] == 1


def test_ops_dashboard_reports_empty_runtime_config_without_active_status(
    tmp_path: Path,
) -> None:
    store = ContentSopRuleProposalStore(tmp_path / "proposals.db")
    overrides_path = tmp_path / "content_sop_rule_overrides.json"
    overrides_path.write_text(
        json.dumps({"version": 1, "rules": [], "audit": []}),
        encoding="utf-8",
    )

    dashboard = build_content_sop_ops_dashboard(
        proposal_store=store,
        overrides_path=overrides_path,
        governed_memory_db_path=tmp_path / "missing.db",
    )

    assert dashboard["runtime_overrides"]["config_ok"] is True
    assert dashboard["runtime_overrides"]["status"] == "configured_empty"
    assert dashboard["runtime_overrides"]["enabled_count"] == 0


def test_ops_reminders_and_card_cover_feishu_admin_actions(tmp_path: Path) -> None:
    store = ContentSopRuleProposalStore(tmp_path / "proposals.db")
    proposal = store.upsert_proposal(_proposal())
    approve_rule_proposal(store, proposal.proposal_id, reviewer="ou_admin")
    overrides_path = tmp_path / "content_sop_rule_overrides.json"
    overrides_path.write_text(
        json.dumps({"version": 1, "rules": [], "audit": []}),
        encoding="utf-8",
    )
    dashboard = build_content_sop_ops_dashboard(
        proposal_store=store,
        overrides_path=overrides_path,
        governed_memory_db_path=tmp_path / "missing.db",
        quality_results=[{"status": "blocked", "score": 40}],
    )

    reminders = build_content_sop_ops_reminders(dashboard)
    card = build_content_sop_ops_reminder_card(reminders)

    assert [item.action for item in reminders] == [
        "confirm_apply_rule",
        "review_quality_gate",
    ]
    assert card["header"]["title"]["content"] == "内容 SOP 运营提醒"
    assert card["header"]["template"] == "red"


def test_ops_reminders_warn_when_runtime_overrides_are_missing(tmp_path: Path) -> None:
    store = ContentSopRuleProposalStore(tmp_path / "proposals.db")
    dashboard = build_content_sop_ops_dashboard(
        proposal_store=store,
        overrides_path=tmp_path / "missing_runtime_overrides.json",
        governed_memory_db_path=tmp_path / "missing.db",
    )

    reminders = build_content_sop_ops_reminders(dashboard)
    card = build_content_sop_ops_reminder_card(reminders)

    assert reminders[0].reminder_id == "runtime_overrides:missing"
    assert reminders[0].action == "review_runtime_overrides"
    assert card["header"]["template"] == "yellow"
    assert dashboard["ops_sop"]["escalation"]


def test_scheduled_ops_exports_audit_report(tmp_path: Path) -> None:
    store = ContentSopRuleProposalStore(tmp_path / "proposals.db")
    store.upsert_proposal(_proposal())

    result = run_scheduled_content_sop_ops(
        {
            "content_sop_ops": {
                "scheduled_import_export_enabled": True,
                "audit_report_enabled": True,
            }
        },
        proposal_store=store,
        overrides_path=tmp_path / "content_sop_rule_overrides.json",
        governed_memory_db_path=tmp_path / "missing.db",
        output_dir=tmp_path / "reports",
        now="2026-06-05T00:00:00Z",
    )

    report_path = Path(result["report_path"])
    assert result["jobs"][0] == {"job": "governance_import_export", "status": "planned"}
    assert report_path.exists()
    assert json.loads(report_path.read_text(encoding="utf-8"))["reminders"]


def test_production_config_confirmation_requires_real_ops_switches(
    tmp_path: Path,
) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "config").mkdir()
    vault = tmp_path / "ObsidianVault"
    vault.mkdir()
    ContentSopRuleProposalStore(tmp_path / "data" / "proposals.db").initialize()
    (tmp_path / "config" / "content_sop_rule_overrides.json").write_text(
        json.dumps({"version": 1, "rules": [], "audit": []}),
        encoding="utf-8",
    )
    memory_store = MemoryGovernanceStore(tmp_path / "data" / "governed_memory.db")
    memory_store.initialize()

    result = confirm_content_sop_production_config(
        {
            "content_sop_rule_review_plugin": {"admin_reviewers": ["ou_admin"]},
            "content_sop_ops": {
                "feishu_reminders_enabled": True,
                "scheduled_import_export_enabled": True,
                "audit_report_enabled": True,
                "quality_minimum_score": 80,
            },
        },
        proposal_store_path=tmp_path / "data" / "proposals.db",
        overrides_path=tmp_path / "config" / "content_sop_rule_overrides.json",
        governed_memory_db_path=tmp_path / "data" / "governed_memory.db",
        obsidian_vault_path=vault,
    )

    assert result["ready"] is True


def test_production_config_confirmation_requires_real_runtime_artifacts(
    tmp_path: Path,
) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "config").mkdir()
    vault = tmp_path / "ObsidianVault"
    vault.mkdir()

    result = confirm_content_sop_production_config(
        {
            "content_sop_rule_review_plugin": {"admin_reviewers": ["ou_admin"]},
            "content_sop_ops": {
                "feishu_reminders_enabled": True,
                "scheduled_import_export_enabled": True,
                "audit_report_enabled": True,
            },
        },
        proposal_store_path=tmp_path / "data" / "proposals.db",
        overrides_path=tmp_path / "config" / "content_sop_rule_overrides.json",
        governed_memory_db_path=tmp_path / "data" / "governed_memory.db",
        obsidian_vault_path=vault,
    )

    assert result["ready"] is False
    assert "quality_minimum_score_confirmed" in result["missing"]
    assert "proposal_store_exists" in result["missing"]
    assert "runtime_overrides_config_exists" in result["missing"]
    assert "governed_memory_store_exists" in result["missing"]


def test_audit_report_export_is_runtime_function_not_test_only_script(
    tmp_path: Path,
) -> None:
    report = export_content_sop_ops_audit_report(
        dashboard={"proposals": {"total": 0}},
        reminders=[],
        output_dir=tmp_path,
        now="2026-06-05T00:00:00Z",
    )

    assert report.name == "content_sop_ops_audit_20260605T000000Z.json"
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["dashboard"]["proposals"]["total"] == 0
