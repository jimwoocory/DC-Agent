from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from typing import Any

from dc_engines.harness.contracts import HarnessTask
from dc_engines.memory_governance.models import GovernedMemory
from dc_engines.memory_governance.store import MemoryGovernanceStore

AUTONOMOUS_ACTIONS = [
    "record_experience",
    "create_memory_candidate",
    "detect_stable_pattern",
    "draft_sop_upgrade_proposal",
    "run_verification_plan",
]
HUMAN_GATED_ACTIONS = [
    "approve_memory",
    "promote_memory_to_runtime",
    "apply_sop_upgrade_to_runtime",
    "change_router_or_harness_rules",
]
SUBAGENT_REVIEW_STAGES = [
    "implementer",
    "spec_compliance_reviewer",
    "code_quality_reviewer",
    "verification_runner",
]
REUSABLE_SIGNAL_PATTERN = re.compile(
    r"以后|这类|这种|每次|默认|固定|统一|原则|习惯|都先|先确认|遇到.*先|不要.*要|别.*要"
)
ONE_OFF_SIGNAL_PATTERN = re.compile(
    r"这句|这一句|这次|当前|临时|先这样|改短|短点|润色一下|帮我改|只改"
)
SCOPE_HINT_PATTERN = re.compile(
    r"客户|共创|邀约|活动|传播|内容|文案|素材|复盘|招商|发布会|私域|飞书"
)


def build_spiral_evolution_seed(payload: dict[str, Any]) -> dict[str, Any]:
    """Build the initial spiral-growth trace for a runtime task payload."""

    return {
        "policy_version": "spiral-evolution-p0",
        "current_stage": "task_experience_recorded",
        "autonomous_actions": list(AUTONOMOUS_ACTIONS),
        "human_gated_actions": list(HUMAN_GATED_ACTIONS),
        "memory_candidate_status": "pending_task_result",
        "sop_upgrade_status": "not_evaluated",
        "subagent_driven_required": True,
        "scope": {
            "workflow_kind": str(payload.get("workflow_kind") or ""),
            "department_id": str(payload.get("department_id") or ""),
            "scenario_id": str(payload.get("scenario_id") or ""),
            "content_type": str(payload.get("content_type") or ""),
        },
    }


def build_spiral_evolution_snapshot(
    task: HarnessTask,
    result: dict[str, Any] | None = None,
    *,
    employee_feedback: str = "",
) -> dict[str, Any]:
    """Create an auditable task -> memory -> proposal spiral snapshot."""

    result = result or {}
    candidate = build_memory_candidate(
        task,
        result=result,
        employee_feedback=employee_feedback,
    )
    return {
        "policy_version": "spiral-evolution-p0",
        "current_stage": "memory_candidate_created",
        "experience": {
            "task_id": task.task_id,
            "domain": task.domain,
            "status": task.status,
            "workflow_kind": str(task.payload.get("workflow_kind") or ""),
            "department_id": str(task.payload.get("department_id") or ""),
            "scenario_id": str(task.payload.get("scenario_id") or ""),
            "content_type": str(task.payload.get("content_type") or ""),
            "material_status": str(task.payload.get("material_status") or ""),
            "generation_allowed": bool(task.payload.get("generation_allowed", False)),
        },
        "memory_candidate": candidate,
        "autonomy_policy": {
            "autonomous_actions": list(AUTONOMOUS_ACTIONS),
            "human_gated_actions": list(HUMAN_GATED_ACTIONS),
            "may_apply_runtime_change_without_review": False,
        },
        "stable_rule_proposal": {
            "status": "not_evaluated",
            "reason": "single need_review candidate cannot become a runtime rule proposal",
            "required_path": "detect_stable_rule_candidates_from_repeated_approved_experiences",
        },
    }


def build_memory_candidate(
    task: HarnessTask,
    *,
    result: dict[str, Any] | None = None,
    employee_feedback: str = "",
) -> dict[str, Any]:
    """Build a need-review memory candidate from a completed task experience."""

    result = result or {}
    payload = task.payload or {}
    citations = _list_dicts(result.get("source_citations")) or _list_dicts(
        payload.get("source_citations")
    )
    canonical_text = _canonical_candidate_text(
        payload=payload,
        result=result,
        employee_feedback=employee_feedback,
    )
    candidate_id = _stable_id(
        "spiral",
        task.task_id,
        str(payload.get("department_id") or ""),
        canonical_text,
    )
    return {
        "candidate_id": candidate_id,
        "memory_kind": _memory_kind(payload, canonical_text, employee_feedback),
        "review_status": "need_review",
        "target_governance": "ObsidianVault/40_MemoryGovernance/Inbox",
        "source_task_id": task.task_id,
        "department_id": str(payload.get("department_id") or ""),
        "scenario_id": str(payload.get("scenario_id") or ""),
        "canonical_text": canonical_text,
        "source_citations": citations,
        "promotion_gate": "obsidian_approved_required",
    }


def analyze_employee_sop_signal(
    message_text: str,
    *,
    department_id: str = "",
    scenario_id: str = "",
    source_task_id: str = "",
    actor_id: str = "",
) -> dict[str, Any]:
    """Classify whether employee chat contains a reusable operating habit."""

    normalized_text = _normalize_rule_text(message_text)
    reusable = bool(REUSABLE_SIGNAL_PATTERN.search(normalized_text))
    one_off = bool(ONE_OFF_SIGNAL_PATTERN.search(normalized_text))
    has_scope = bool(department_id and scenario_id) or bool(
        SCOPE_HINT_PATTERN.search(normalized_text)
    )
    if not normalized_text:
        return {
            "status": "ignored",
            "reason": "empty_message",
            "confidence": 0.0,
        }
    if one_off and not reusable:
        return {
            "status": "ignored",
            "reason": "one_off_or_low_reuse_signal",
            "confidence": 0.2,
            "message_text": normalized_text,
        }
    if not reusable or not has_scope:
        return {
            "status": "ignored",
            "reason": "missing_reusable_intent_or_scope",
            "confidence": 0.35 if reusable or has_scope else 0.1,
            "message_text": normalized_text,
        }
    return {
        "status": "candidate",
        "signal_type": "reusable_operating_habit",
        "confidence": 0.9 if department_id and scenario_id else 0.75,
        "department_id": department_id,
        "scenario_id": scenario_id,
        "source_task_id": source_task_id,
        "actor_id": actor_id,
        "rule_text": normalized_text[:500],
        "message_text": normalized_text,
        "runtime_change_allowed": False,
    }


def build_low_friction_sop_confirmation(
    signal: dict[str, Any],
    *,
    employee_name: str = "",
) -> dict[str, Any]:
    """Build an employee-facing confirmation without internal SOP terminology."""

    rule_text = str(signal.get("rule_text") or signal.get("message_text") or "").strip()
    title_prefix = f"{employee_name}，" if employee_name else ""
    return {
        "title": f"{title_prefix}这个处理习惯要不要记住？",
        "message": (
            "我理解为：以后遇到类似任务，先按这个方式帮你处理："
            f"{rule_text}\n\n"
            "你点一下就行，我不会直接改系统设置。"
        ),
        "choices": ["记住", "只这次", "不用"],
        "default_choice": "记住",
        "runtime_change_allowed": False,
    }


def build_employee_sop_memory_candidate(signal: dict[str, Any]) -> dict[str, Any]:
    """Convert a confirmed employee signal into a governed memory candidate."""

    if str(signal.get("status") or "") != "candidate":
        raise ValueError("signal must be a candidate")
    rule_text = _normalize_rule_text(str(signal.get("rule_text") or ""))
    department_id = str(signal.get("department_id") or "")
    scenario_id = str(signal.get("scenario_id") or "")
    source_task_id = str(signal.get("source_task_id") or "")
    candidate_id = _stable_id(
        "employee_sop",
        source_task_id,
        department_id,
        scenario_id,
        rule_text,
    )
    tags = [
        "source:employee_chat",
        "signal_type:reusable_operating_habit",
    ]
    if department_id:
        tags.append(f"department_id:{department_id}")
    if scenario_id:
        tags.append(f"scenario_id:{scenario_id}")
    return {
        "candidate_id": candidate_id,
        "memory_kind": "process_memory",
        "review_status": "need_review",
        "target_governance": "ObsidianVault/40_MemoryGovernance/Inbox",
        "source_task_id": source_task_id,
        "source_actor_id": str(signal.get("actor_id") or ""),
        "department_id": department_id,
        "scenario_id": scenario_id,
        "canonical_text": rule_text,
        "rule_text": rule_text,
        "confidence": float(signal.get("confidence") or 0.0),
        "tags": tags,
        "promotion_gate": "obsidian_approved_required",
        "runtime_change_allowed": False,
    }


def detect_stable_rule_candidates(
    experiences: list[dict[str, Any]],
    *,
    min_support: int = 3,
) -> list[dict[str, Any]]:
    """Detect repeated reviewed corrections that deserve a SOP upgrade proposal."""

    buckets: dict[tuple[str, str, str], dict[str, dict[str, Any]]] = defaultdict(dict)
    for item in experiences:
        department_id = str(item.get("department_id") or "")
        scenario_id = str(item.get("scenario_id") or "")
        rule_text = _normalize_rule_text(str(item.get("rule_text") or ""))
        if not department_id or not rule_text:
            continue
        if str(item.get("review_status") or "") != "approved":
            continue
        evidence_id = str(item.get("candidate_id") or item.get("source_task_id") or "")
        if not evidence_id:
            continue
        buckets[(department_id, scenario_id, rule_text)][evidence_id] = item

    proposals: list[dict[str, Any]] = []
    for (department_id, scenario_id, rule_text), evidence_by_id in sorted(
        buckets.items()
    ):
        evidence = list(evidence_by_id.values())
        if len(evidence) < min_support:
            continue
        candidate_ids = [
            str(item.get("candidate_id") or "")
            for item in evidence
            if str(item.get("candidate_id") or "")
        ]
        proposal = {
            "proposal_id": _stable_id(
                "proposal",
                department_id,
                scenario_id,
                rule_text,
                ",".join(candidate_ids),
            ),
            "department_id": department_id,
            "scenario_id": scenario_id,
            "rule_text": rule_text,
            "support_count": len(evidence),
            "evidence_candidate_ids": candidate_ids,
            "status": "pending",
        }
        proposal["subagent_driven_upgrade"] = build_subagent_driven_upgrade_plan(
            proposal
        )
        proposals.append(proposal)
    return proposals


def diagnose_rule_proposal_readiness(
    experiences: list[dict[str, Any]],
    *,
    min_support: int = 3,
) -> dict[str, Any]:
    """Explain why approved memories can or cannot draft stable SOP proposals."""

    ineligible_counts = {
        "missing_scope": 0,
        "missing_rule_text": 0,
        "unapproved": 0,
        "missing_evidence_id": 0,
    }
    buckets: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for item in experiences:
        department_id = str(item.get("department_id") or "")
        scenario_id = str(item.get("scenario_id") or "")
        rule_text = _normalize_rule_text(str(item.get("rule_text") or ""))
        review_status = str(item.get("review_status") or "")
        evidence_id = str(item.get("candidate_id") or item.get("source_task_id") or "")
        if not department_id:
            ineligible_counts["missing_scope"] += 1
            continue
        if not rule_text:
            ineligible_counts["missing_rule_text"] += 1
            continue
        if review_status != "approved":
            ineligible_counts["unapproved"] += 1
            continue
        if not evidence_id:
            ineligible_counts["missing_evidence_id"] += 1
            continue
        buckets[(department_id, scenario_id, rule_text)].add(evidence_id)

    bucket_rows = []
    ready_count = 0
    for (department_id, scenario_id, rule_text), evidence_ids in sorted(
        buckets.items()
    ):
        support_count = len(evidence_ids)
        ready = support_count >= min_support
        if ready:
            ready_count += 1
        bucket_rows.append(
            {
                "department_id": department_id,
                "scenario_id": scenario_id,
                "rule_text": rule_text,
                "support_count": support_count,
                "support_needed": max(min_support - support_count, 0),
                "ready": ready,
                "evidence_candidate_ids": sorted(evidence_ids),
            }
        )
    return {
        "min_support": min_support,
        "eligible_evidence_count": sum(
            int(row["support_count"]) for row in bucket_rows
        ),
        "ready_count": ready_count,
        "ineligible_counts": ineligible_counts,
        "buckets": bucket_rows,
    }


def experiences_from_governed_memories(
    memories: list[GovernedMemory],
) -> list[dict[str, Any]]:
    """Convert approved governed process memories into rule-detection evidence."""

    experiences: list[dict[str, Any]] = []
    for memory in memories:
        if memory.review_status != "approved":
            continue
        if memory.memory_kind not in {"process", "process_memory"}:
            continue
        department_id = _tag_value(memory.tags, "department_id") or _tag_value(
            memory.tags, "department"
        )
        if not department_id:
            continue
        rule_text = _normalize_rule_text(memory.canonical_text)
        if not rule_text:
            continue
        experiences.append(
            {
                "candidate_id": memory.memory_id,
                "source_task_id": memory.source_id
                if memory.source_system == "harness"
                else "",
                "department_id": department_id,
                "scenario_id": _tag_value(memory.tags, "scenario_id")
                or _tag_value(memory.tags, "scenario"),
                "rule_text": rule_text,
                "review_status": memory.review_status,
                "source_path": memory.source_path or memory.obsidian_note_path,
                "approved_at": memory.approved_at,
                "approved_by": memory.approved_by,
            }
        )
    return experiences


def draft_content_sop_rule_proposals_from_governed_memory(
    *,
    memory_store: MemoryGovernanceStore,
    proposal_store: Any,
    min_support: int = 3,
    actor: str = "spiral_evolution",
    limit: int = 10000,
) -> list[Any]:
    """Draft pending Content SOP rule proposals from repeated approved memories."""

    if min_support < 2:
        raise ValueError("min_support must be at least 2")
    memory_store.initialize()
    memories = memory_store.list_memories(status="approved", limit=limit)
    proposals = detect_stable_rule_candidates(
        experiences_from_governed_memories(memories),
        min_support=min_support,
    )
    stored: list[Any] = []
    for proposal in proposals:
        existing = proposal_store.get_proposal(str(proposal.get("proposal_id") or ""))
        if existing is not None and existing.status != "pending":
            continue
        if existing is not None and existing.source_payload == proposal:
            continue
        persisted = proposal_store.upsert_proposal(proposal, status="pending")
        if existing is None or existing.source_payload != persisted.source_payload:
            proposal_store.record_audit(
                persisted.proposal_id,
                "drafted_from_approved_memories",
                actor=actor,
                detail={
                    "support_count": persisted.support_count,
                    "evidence_candidate_ids": persisted.evidence_candidate_ids,
                    "subagent_driven_required": True,
                    "runtime_change_allowed": False,
                },
            )
        stored.append(persisted)
    return stored


def build_subagent_driven_upgrade_plan(proposal: dict[str, Any]) -> dict[str, Any]:
    """Represent the gated subagent-driven path from proposal to runtime rule."""

    proposal_id = str(proposal.get("proposal_id") or _stable_id("proposal", proposal))
    return {
        "proposal_id": proposal_id,
        "status": "not_applied",
        "runtime_change_allowed": False,
        "required_stages": [
            {
                "stage": "implementer",
                "agent_role": "worker",
                "required_output": "bounded_patch_or_noop_with_reason",
            },
            {
                "stage": "spec_compliance_reviewer",
                "agent_role": "reviewer",
                "required_output": "spec_compliance_approval",
            },
            {
                "stage": "code_quality_reviewer",
                "agent_role": "reviewer",
                "required_output": "code_quality_approval",
            },
            {
                "stage": "verification_runner",
                "agent_role": "controller",
                "required_output": "targeted_gate_passed",
            },
        ],
        "acceptance_gates": [
            "contract_criteria_updated",
            "runtime_entrypoint_connected",
            "regression_test_added",
            "targeted_gate_passed",
            "repository_hygiene_passed",
        ],
    }


def _canonical_candidate_text(
    *,
    payload: dict[str, Any],
    result: dict[str, Any],
    employee_feedback: str,
) -> str:
    feedback = employee_feedback.strip()
    if feedback:
        return feedback[:500]
    for key in (
        "review_learnings",
        "summary",
        "message_draft",
        "video_script",
        "review_checklist",
    ):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:500]
        if isinstance(value, list) and value:
            return "；".join(str(item) for item in value if str(item).strip())[:500]
    brief = str(payload.get("brief") or payload.get("message_text") or "").strip()
    return brief[:500]


def _memory_kind(
    payload: dict[str, Any],
    canonical_text: str,
    employee_feedback: str,
) -> str:
    text = f"{employee_feedback} {canonical_text} {payload.get('brief') or ''}"
    if re.search(r"SOP|流程|默认|不要|禁止|必须|习惯|规则|邮件|飞书|私域", text):
        return "process_memory"
    if re.search(r"喜欢|偏好|口径|语气|称呼", text):
        return "preference_memory"
    return "fact_memory"


def _list_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _normalize_rule_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _tag_value(tags: list[str], key: str) -> str:
    prefix = f"{key}:"
    for tag in tags:
        item = str(tag or "").strip()
        if item.startswith(prefix):
            return item[len(prefix) :].strip()
    return ""


def _stable_id(prefix: str, *parts: Any) -> str:
    raw = "|".join(str(part) for part in parts)
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"
