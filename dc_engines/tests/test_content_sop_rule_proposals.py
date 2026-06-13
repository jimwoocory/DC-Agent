from __future__ import annotations

import pytest
from dc_engines.department_workflows.content_rule_proposals import (
    ContentSopRuleProposalStore,
    apply_approved_rule_proposal,
    approve_rule_proposal,
    build_rule_proposal_review_card,
    reject_rule_proposal,
    rollback_applied_rule_proposal,
)


def _proposal() -> dict:
    return {
        "proposal_id": "proposal_no_email",
        "department_id": "client_dept",
        "scenario_id": "customer_greeting",
        "rule_type": "process",
        "rule_text": "客户触达默认使用飞书和私域，不默认使用邮件。",
        "support_count": 3,
        "evidence_candidate_ids": ["cand_1", "cand_2", "cand_3"],
        "status": "pending",
    }


def test_rule_proposal_store_upserts_pending_only(tmp_path) -> None:
    store = ContentSopRuleProposalStore(tmp_path / "proposals.db")

    proposal = store.upsert_proposal(_proposal())
    approve_rule_proposal(
        store,
        proposal.proposal_id,
        reviewer="ou_admin",
        allowed_reviewers={"ou_admin"},
    )
    store.upsert_proposal(
        {**_proposal(), "rule_text": "被忽略的新规则"},
    )

    reloaded = store.get_proposal(proposal.proposal_id)
    assert reloaded is not None
    assert reloaded.status == "approved_for_runtime"
    assert reloaded.rule_text == "客户触达默认使用飞书和私域，不默认使用邮件。"


def test_rule_proposal_requires_allowed_reviewer(tmp_path) -> None:
    store = ContentSopRuleProposalStore(tmp_path / "proposals.db")
    proposal = store.upsert_proposal(_proposal())

    with pytest.raises(PermissionError):
        approve_rule_proposal(
            store,
            proposal.proposal_id,
            reviewer="ou_user",
            allowed_reviewers={"ou_admin"},
        )


def test_approve_apply_and_rollback_rule_proposal(tmp_path) -> None:
    store = ContentSopRuleProposalStore(tmp_path / "proposals.db")
    overrides_path = tmp_path / "content_sop_rule_overrides.json"
    proposal = store.upsert_proposal(_proposal())

    approve_rule_proposal(
        store,
        proposal.proposal_id,
        reviewer="ou_admin",
        allowed_reviewers={"ou_admin"},
    )
    applied = apply_approved_rule_proposal(
        store,
        proposal.proposal_id,
        overrides_path=overrides_path,
        reviewer="ou_admin",
        allowed_reviewers={"ou_admin"},
        now="2026-06-05T00:00:00Z",
    )
    rolled_back = rollback_applied_rule_proposal(
        store,
        proposal.proposal_id,
        overrides_path=overrides_path,
        reviewer="ou_admin",
        allowed_reviewers={"ou_admin"},
        now="2026-06-05T00:01:00Z",
    )

    final = store.get_proposal(proposal.proposal_id)
    assert applied.applied is True
    assert rolled_back.applied is True
    assert final is not None
    assert final.status == "rolled_back"
    assert [event.action for event in store.list_audit(proposal.proposal_id)] == [
        "approved_for_runtime",
        "applied",
        "rolled_back",
    ]


def test_apply_requires_approved_for_runtime(tmp_path) -> None:
    store = ContentSopRuleProposalStore(tmp_path / "proposals.db")
    proposal = store.upsert_proposal(_proposal())

    with pytest.raises(ValueError, match="approved_for_runtime"):
        apply_approved_rule_proposal(
            store,
            proposal.proposal_id,
            overrides_path=tmp_path / "content_sop_rule_overrides.json",
            reviewer="ou_admin",
            allowed_reviewers={"ou_admin"},
        )


def test_reject_rule_proposal_prevents_apply(tmp_path) -> None:
    store = ContentSopRuleProposalStore(tmp_path / "proposals.db")
    proposal = store.upsert_proposal(_proposal())

    reject_rule_proposal(
        store,
        proposal.proposal_id,
        reviewer="ou_admin",
        allowed_reviewers={"ou_admin"},
        reason="not company SOP",
    )

    with pytest.raises(ValueError, match="approved_for_runtime"):
        apply_approved_rule_proposal(
            store,
            proposal.proposal_id,
            overrides_path=tmp_path / "content_sop_rule_overrides.json",
            reviewer="ou_admin",
            allowed_reviewers={"ou_admin"},
        )


def test_stale_actions_cannot_change_applied_proposal(tmp_path) -> None:
    store = ContentSopRuleProposalStore(tmp_path / "proposals.db")
    proposal = store.upsert_proposal(_proposal())
    approve_rule_proposal(store, proposal.proposal_id, reviewer="ou_admin")
    apply_approved_rule_proposal(
        store,
        proposal.proposal_id,
        overrides_path=tmp_path / "content_sop_rule_overrides.json",
        reviewer="ou_admin",
    )

    with pytest.raises(ValueError, match="current status is applied"):
        reject_rule_proposal(store, proposal.proposal_id, reviewer="ou_admin")
    with pytest.raises(ValueError, match="current status is applied"):
        approve_rule_proposal(store, proposal.proposal_id, reviewer="ou_admin")

    reloaded = store.get_proposal(proposal.proposal_id)
    assert reloaded is not None
    assert reloaded.status == "applied"


def test_rollback_requires_applied_proposal_before_runtime_change(tmp_path) -> None:
    store = ContentSopRuleProposalStore(tmp_path / "proposals.db")
    overrides_path = tmp_path / "content_sop_rule_overrides.json"
    proposal = store.upsert_proposal(_proposal())
    approve_rule_proposal(store, proposal.proposal_id, reviewer="ou_admin")
    apply_approved_rule_proposal(
        store,
        proposal.proposal_id,
        overrides_path=overrides_path,
        reviewer="ou_admin",
    )
    store.set_status(
        proposal.proposal_id,
        "rolled_back",
        actor="test",
        from_statuses={"applied"},
    )
    before = overrides_path.read_text(encoding="utf-8")

    with pytest.raises(ValueError, match="applied before rollback"):
        rollback_applied_rule_proposal(
            store,
            proposal.proposal_id,
            overrides_path=overrides_path,
            reviewer="ou_admin",
        )

    assert overrides_path.read_text(encoding="utf-8") == before


def test_rollback_missing_proposal_does_not_change_runtime_override(tmp_path) -> None:
    store = ContentSopRuleProposalStore(tmp_path / "proposals.db")
    overrides_path = tmp_path / "content_sop_rule_overrides.json"
    proposal = store.upsert_proposal(_proposal())
    approve_rule_proposal(store, proposal.proposal_id, reviewer="ou_admin")
    apply_approved_rule_proposal(
        store,
        proposal.proposal_id,
        overrides_path=overrides_path,
        reviewer="ou_admin",
    )
    missing_store = ContentSopRuleProposalStore(tmp_path / "missing.db")
    before = overrides_path.read_text(encoding="utf-8")

    with pytest.raises(LookupError):
        rollback_applied_rule_proposal(
            missing_store,
            proposal.proposal_id,
            overrides_path=overrides_path,
            reviewer="ou_admin",
        )

    assert overrides_path.read_text(encoding="utf-8") == before


def test_duplicate_approve_click_does_not_duplicate_or_corrupt_status(tmp_path) -> None:
    store = ContentSopRuleProposalStore(tmp_path / "proposals.db")
    proposal = store.upsert_proposal(_proposal())

    approve_rule_proposal(store, proposal.proposal_id, reviewer="ou_admin")
    with pytest.raises(ValueError, match="current status is approved_for_runtime"):
        approve_rule_proposal(store, proposal.proposal_id, reviewer="ou_admin")

    assert store.get_proposal(proposal.proposal_id).status == "approved_for_runtime"  # type: ignore[union-attr]
    assert [event.action for event in store.list_audit(proposal.proposal_id)] == [
        "approved_for_runtime"
    ]


def test_rule_proposal_review_card_uses_namespaced_actions(tmp_path) -> None:
    store = ContentSopRuleProposalStore(tmp_path / "proposals.db")
    proposal = store.upsert_proposal(_proposal())

    card = build_rule_proposal_review_card(proposal)
    actions = card["elements"][1]["actions"]

    assert actions[0]["value"] == {
        "source": "content_sop_rule_review",
        "proposal_id": "proposal_no_email",
        "action": "content_sop_rule_approve_apply",
    }
    assert actions[1]["value"]["action"] == "content_sop_rule_reject"
    assert actions[2]["value"]["action"] == "content_sop_rule_rollback"
