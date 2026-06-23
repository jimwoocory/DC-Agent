from __future__ import annotations

from dc_engines.department_workflows import (
    ContentSopRuleProposalStore,
    build_content_sop_workflow_payload,
    match_department_workflow,
)
from dc_engines.harness.contracts import HarnessTask
from dc_engines.memory_governance.models import GovernedMemory
from dc_engines.memory_governance.store import MemoryGovernanceStore
from dc_engines.spiral_evolution import (
    analyze_employee_sop_signal,
    build_employee_sop_memory_candidate,
    build_low_friction_sop_confirmation,
    build_spiral_evolution_snapshot,
    build_subagent_driven_upgrade_plan,
    detect_stable_rule_candidates,
    diagnose_rule_proposal_readiness,
    draft_content_sop_rule_proposals_from_governed_memory,
    experiences_from_governed_memories,
)


def _task(payload: dict) -> HarnessTask:
    return HarnessTask(
        task_id="task_spiral_001",
        conversation_id="conv_spiral",
        platform_id="巅池-Agent小助手",
        session_id="lark:user",
        title="内容 SOP | 客户部",
        domain="content_sop:client_dept",
        status="in_progress",
        payload=payload,
        result={},
        created_at="2026-06-05T00:00:00Z",
        updated_at="2026-06-05T00:00:00Z",
    )


def _content_sop_payload() -> dict:
    match = match_department_workflow(
        employee_department="客户部",
        text="帮我写客户邀约文案并配图",
        min_score=1,
    )
    assert match is not None
    return build_content_sop_workflow_payload(
        match,
        source="test",
        message_text="帮我写客户邀约文案并配图",
        content_type="mixed",
        knowledge_context="Obsidian 已治理记忆: 公司客户触达默认使用飞书和私域。",
        source_citations=[
            {
                "type": "governed_memory",
                "source_path": "ObsidianVault/40_MemoryGovernance/Approved/mem_001.md",
            }
        ],
    )


def test_content_sop_payload_carries_spiral_seed() -> None:
    payload = _content_sop_payload()

    spiral = payload["spiral_evolution"]

    assert spiral["current_stage"] == "task_experience_recorded"
    assert spiral["subagent_driven_required"] is True
    assert "record_experience" in spiral["autonomous_actions"]
    assert "apply_sop_upgrade_to_runtime" in spiral["human_gated_actions"]
    assert spiral["scope"]["department_id"] == "client_dept"


def test_content_sop_payload_protects_spiral_scope_from_requester_meta() -> None:
    match = match_department_workflow(
        employee_department="客户部",
        text="帮我写客户邀约文案并配图",
        min_score=1,
    )
    assert match is not None

    payload = build_content_sop_workflow_payload(
        match,
        source="test",
        message_text="帮我写客户邀约文案并配图",
        content_type="mixed",
        requester_meta={
            "requester_open_id": "ou_001",
            "department_id": "planning",
            "scenario_id": "wrong",
            "spiral_evolution": {"scope": {"department_id": "wrong"}},
        },
    )

    assert payload["requester_open_id"] == "ou_001"
    assert payload["department_id"] == "client_dept"
    assert payload["scenario_id"] != "wrong"
    assert payload["spiral_evolution"]["scope"]["department_id"] == "client_dept"


def test_spiral_snapshot_creates_need_review_memory_candidate() -> None:
    task = _task(_content_sop_payload())

    snapshot = build_spiral_evolution_snapshot(
        task,
        {
            "message_draft": "客户触达默认走飞书和私域，不写邮件。",
            "source_citations": [
                {"source_path": "ObsidianVault/40_MemoryGovernance/Approved/mem_001.md"}
            ],
        },
    )

    candidate = snapshot["memory_candidate"]
    assert snapshot["current_stage"] == "memory_candidate_created"
    assert candidate["review_status"] == "need_review"
    assert candidate["promotion_gate"] == "obsidian_approved_required"
    assert candidate["target_governance"] == "ObsidianVault/40_MemoryGovernance/Inbox"
    assert candidate["source_task_id"] == task.task_id
    assert candidate["department_id"] == "client_dept"
    assert candidate["memory_kind"] == "process_memory"
    assert candidate["source_citations"][0]["source_path"].endswith("mem_001.md")
    assert (
        snapshot["autonomy_policy"]["may_apply_runtime_change_without_review"] is False
    )


def test_detect_stable_rule_candidates_requires_approved_support() -> None:
    experiences = [
        {
            "candidate_id": f"cand_{index}",
            "department_id": "client_dept",
            "scenario_id": "client_invitation_copy",
            "rule_text": "客户触达默认使用飞书和私域，不默认使用邮件。",
            "review_status": "approved",
        }
        for index in range(3)
    ]
    experiences.append(
        {
            "candidate_id": "cand_unreviewed",
            "department_id": "client_dept",
            "scenario_id": "client_invitation_copy",
            "rule_text": "客户触达默认使用飞书和私域，不默认使用邮件。",
            "review_status": "need_review",
        }
    )

    proposals = detect_stable_rule_candidates(experiences, min_support=3)

    assert len(proposals) == 1
    assert proposals[0]["support_count"] == 3
    assert proposals[0]["status"] == "pending"
    assert proposals[0]["rule_text"] == "客户触达默认使用飞书和私域，不默认使用邮件。"


def test_detect_stable_rule_candidates_dedupes_evidence_ids() -> None:
    duplicate_experiences = [
        {
            "candidate_id": "cand_same",
            "department_id": "client_dept",
            "scenario_id": "client_invitation_copy",
            "rule_text": "客户触达默认使用飞书和私域，不默认使用邮件。",
            "review_status": "approved",
        }
        for _ in range(3)
    ]

    proposals = detect_stable_rule_candidates(duplicate_experiences, min_support=3)

    assert proposals == []


def test_subagent_driven_upgrade_plan_requires_reviews_and_gates() -> None:
    plan = build_subagent_driven_upgrade_plan(
        {
            "proposal_id": "proposal_no_email",
            "department_id": "client_dept",
            "rule_text": "客户触达不默认使用邮件。",
        }
    )

    stages = [stage["stage"] for stage in plan["required_stages"]]

    assert stages == [
        "implementer",
        "spec_compliance_reviewer",
        "code_quality_reviewer",
        "verification_runner",
    ]
    assert plan["runtime_change_allowed"] is False
    assert "targeted_gate_passed" in plan["acceptance_gates"]
    assert "repository_hygiene_passed" in plan["acceptance_gates"]


def _governed_memory(
    memory_id: str,
    *,
    review_status: str = "approved",
    memory_kind: str = "process",
    rule_text: str = "客户触达默认使用飞书和私域，不默认使用邮件。",
    tags: list[str] | None = None,
) -> GovernedMemory:
    return GovernedMemory(
        memory_id=memory_id,
        source_system="harness",
        source_id=f"task_{memory_id}",
        source_path=f"ObsidianVault/40_MemoryGovernance/Approved/{memory_id}.md",
        source_hash=f"hash_{memory_id}",
        title=f"规则记忆 {memory_id}",
        summary=rule_text,
        canonical_text=rule_text,
        memory_kind=memory_kind,  # type: ignore[arg-type]
        review_status=review_status,  # type: ignore[arg-type]
        confidence=0.9,
        sensitivity="internal",
        tags=tags or ["department_id:client_dept", "scenario_id:customer_greeting"],
        created_at="2026-06-05T00:00:00Z",
        updated_at="2026-06-05T00:00:00Z",
        approved_at="2026-06-05T00:01:00Z" if review_status == "approved" else "",
        approved_by="ou_admin" if review_status == "approved" else "",
    )


def test_experiences_from_governed_memories_requires_approved_process_scope() -> None:
    experiences = experiences_from_governed_memories(
        [
            _governed_memory("mem_1"),
            _governed_memory("mem_unreviewed", review_status="need_review"),
            _governed_memory("mem_fact", memory_kind="fact"),
            _governed_memory("mem_no_department", tags=["scenario_id:customer"]),
        ]
    )

    assert len(experiences) == 1
    assert experiences[0]["candidate_id"] == "mem_1"
    assert experiences[0]["department_id"] == "client_dept"
    assert experiences[0]["review_status"] == "approved"


def test_draft_rule_proposals_from_approved_governed_memory_store(tmp_path) -> None:
    memory_store = MemoryGovernanceStore(tmp_path / "governed_memory.db")
    memory_store.initialize()
    proposal_store = ContentSopRuleProposalStore(tmp_path / "rule_proposals.db")
    for index in range(3):
        memory_store.upsert_memory(_governed_memory(f"mem_{index}"))
    memory_store.upsert_memory(
        _governed_memory("mem_unreviewed", review_status="need_review")
    )

    proposals = draft_content_sop_rule_proposals_from_governed_memory(
        memory_store=memory_store,
        proposal_store=proposal_store,
        min_support=3,
        actor="spiral_evolution_test",
    )

    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal.status == "pending"
    assert proposal.support_count == 3
    assert proposal.evidence_candidate_ids == ["mem_0", "mem_1", "mem_2"]
    assert proposal_store.list_audit(proposal.proposal_id)[0].action == (
        "drafted_from_approved_memories"
    )


def test_draft_rule_proposals_does_not_overwrite_reviewed_proposal(tmp_path) -> None:
    memory_store = MemoryGovernanceStore(tmp_path / "governed_memory.db")
    memory_store.initialize()
    proposal_store = ContentSopRuleProposalStore(tmp_path / "rule_proposals.db")
    for index in range(3):
        memory_store.upsert_memory(_governed_memory(f"mem_{index}"))
    proposals = draft_content_sop_rule_proposals_from_governed_memory(
        memory_store=memory_store,
        proposal_store=proposal_store,
        min_support=3,
    )
    proposal_store.set_status(
        proposals[0].proposal_id,
        "rejected",
        actor="ou_admin",
        from_statuses={"pending"},
    )

    redrafted = draft_content_sop_rule_proposals_from_governed_memory(
        memory_store=memory_store,
        proposal_store=proposal_store,
        min_support=3,
    )

    assert redrafted == []
    assert proposal_store.get_proposal(proposals[0].proposal_id).status == "rejected"  # type: ignore[union-attr]


def test_employee_chat_signal_requires_reusable_scope() -> None:
    signal = analyze_employee_sop_signal(
        "以后这种客户共创会邀约，先确认对方是谁、活动目的和飞书私域口吻，再出文案。",
        department_id="client_dept",
        scenario_id="client_invitation_copy",
        source_task_id="task_001",
        actor_id="ou_employee",
    )

    assert signal["status"] == "candidate"
    assert signal["signal_type"] == "reusable_operating_habit"
    assert signal["department_id"] == "client_dept"
    assert signal["scenario_id"] == "client_invitation_copy"
    assert signal["source_task_id"] == "task_001"
    assert signal["confidence"] >= 0.8
    assert "先确认对方是谁" in signal["rule_text"]


def test_one_off_wording_edit_is_not_sop_signal() -> None:
    signal = analyze_employee_sop_signal(
        "这句帮我改短点，别那么正式。",
        department_id="client_dept",
        scenario_id="client_invitation_copy",
        source_task_id="task_002",
    )

    assert signal["status"] == "ignored"
    assert signal["reason"] == "one_off_or_low_reuse_signal"


def test_employee_confirmation_is_low_friction_and_no_sop_jargon() -> None:
    signal = analyze_employee_sop_signal(
        "以后这种客户共创会邀约，先确认对方是谁、活动目的和飞书私域口吻，再出文案。",
        department_id="client_dept",
        scenario_id="client_invitation_copy",
    )

    confirmation = build_low_friction_sop_confirmation(signal)

    assert confirmation["title"] == "这个处理习惯要不要记住？"
    assert confirmation["choices"] == ["记住", "只这次", "不用"]
    rendered = f"{confirmation['title']}\n{confirmation['message']}"
    assert "蔡挺" not in rendered
    assert "SOP" not in rendered
    assert "规则候选" not in rendered
    assert "流程" not in rendered
    assert "飞书私域口吻" in rendered


def test_employee_signal_builds_need_review_process_memory_candidate() -> None:
    signal = analyze_employee_sop_signal(
        "以后这种客户共创会邀约，先确认对方是谁、活动目的和飞书私域口吻，再出文案。",
        department_id="client_dept",
        scenario_id="client_invitation_copy",
        source_task_id="task_003",
        actor_id="ou_employee",
    )

    candidate = build_employee_sop_memory_candidate(signal)

    assert candidate["memory_kind"] == "process_memory"
    assert candidate["review_status"] == "need_review"
    assert candidate["promotion_gate"] == "obsidian_approved_required"
    assert candidate["target_governance"] == "ObsidianVault/40_MemoryGovernance/Inbox"
    assert candidate["source_task_id"] == "task_003"
    assert candidate["department_id"] == "client_dept"
    assert candidate["scenario_id"] == "client_invitation_copy"
    assert "department_id:client_dept" in candidate["tags"]
    assert "scenario_id:client_invitation_copy" in candidate["tags"]
    assert candidate["runtime_change_allowed"] is False


def test_rule_proposal_readiness_diagnoses_support_gap() -> None:
    readiness = diagnose_rule_proposal_readiness(
        [
            {
                "candidate_id": "cand_1",
                "department_id": "client_dept",
                "scenario_id": "client_invitation_copy",
                "rule_text": "客户共创会邀约先确认对象、目的和飞书私域口吻。",
                "review_status": "approved",
            },
            {
                "candidate_id": "cand_2",
                "department_id": "client_dept",
                "scenario_id": "client_invitation_copy",
                "rule_text": "客户共创会邀约先确认对象、目的和飞书私域口吻。",
                "review_status": "approved",
            },
            {
                "candidate_id": "cand_unapproved",
                "department_id": "client_dept",
                "scenario_id": "client_invitation_copy",
                "rule_text": "客户共创会邀约先确认对象、目的和飞书私域口吻。",
                "review_status": "need_review",
            },
            {
                "candidate_id": "cand_no_scope",
                "department_id": "",
                "scenario_id": "client_invitation_copy",
                "rule_text": "客户共创会邀约先确认对象、目的和飞书私域口吻。",
                "review_status": "approved",
            },
        ],
        min_support=3,
    )

    assert readiness["ready_count"] == 0
    assert readiness["eligible_evidence_count"] == 2
    assert readiness["ineligible_counts"]["unapproved"] == 1
    assert readiness["ineligible_counts"]["missing_scope"] == 1
    bucket = readiness["buckets"][0]
    assert bucket["support_count"] == 2
    assert bucket["support_needed"] == 1
    assert bucket["ready"] is False
