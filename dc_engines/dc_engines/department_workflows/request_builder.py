from __future__ import annotations

from typing import Any

from dc_engines.harness import HarnessTaskCreateRequest

from .channel_policy import communication_channel_policy_for
from .contracts import (
    DepartmentWorkflow,
    DepartmentWorkflowMatch,
    MaterialIntakeAssessment,
    OutputSpec,
    RequiredInput,
    Scenario,
)
from .materials import assess_material_intake
from .quality_gate import (
    build_content_sop_quality_policy,
    evaluate_content_sop_payload,
)


def build_department_workflow_request(
    match: DepartmentWorkflowMatch,
    *,
    conversation_id: str,
    platform_id: str,
    session_id: str,
    source: str,
    message_text: str,
    requester_meta: dict[str, Any] | None = None,
    material_assessment: MaterialIntakeAssessment | None = None,
) -> HarnessTaskCreateRequest:
    workflow = match.workflow
    scenario = match.scenario
    payload = build_department_workflow_payload(
        match,
        source=source,
        message_text=message_text,
        requester_meta=requester_meta,
        material_assessment=material_assessment,
    )
    return HarnessTaskCreateRequest(
        title=_build_title(workflow, scenario, message_text),
        conversation_id=conversation_id,
        platform_id=platform_id,
        session_id=session_id,
        domain=f"department_workflow:{workflow.department_id}",
        payload=payload,
    )


def build_content_sop_workflow_request(
    match: DepartmentWorkflowMatch,
    *,
    conversation_id: str,
    platform_id: str,
    session_id: str,
    source: str,
    message_text: str,
    content_type: str,
    requester_meta: dict[str, Any] | None = None,
    knowledge_context: str = "",
    source_citations: list[dict[str, str]] | None = None,
    material_assessment: MaterialIntakeAssessment | None = None,
) -> HarnessTaskCreateRequest:
    payload = build_content_sop_workflow_payload(
        match,
        source=source,
        message_text=message_text,
        content_type=content_type,
        requester_meta=requester_meta,
        knowledge_context=knowledge_context,
        source_citations=source_citations,
        material_assessment=material_assessment,
    )
    return HarnessTaskCreateRequest(
        title=_build_content_sop_title(match.workflow, match.scenario, message_text),
        conversation_id=conversation_id,
        platform_id=platform_id,
        session_id=session_id,
        domain=f"content_sop:{match.workflow.department_id}",
        payload=payload,
    )


def build_department_workflow_payload(
    match: DepartmentWorkflowMatch,
    *,
    source: str,
    message_text: str,
    requester_meta: dict[str, Any] | None = None,
    material_assessment: MaterialIntakeAssessment | None = None,
) -> dict[str, Any]:
    workflow = match.workflow
    scenario = match.scenario
    material_assessment = material_assessment or assess_material_intake(
        scenario,
        message_text,
    )
    payload: dict[str, Any] = {
        "source": source,
        "workflow_kind": "department_workflow",
        "department": workflow.department_name,
        "department_id": workflow.department_id,
        "scenario_id": scenario.scenario_id,
        "scenario_name": scenario.name,
        "brief": message_text.strip()[:500],
        "message_text": message_text,
        "match_score": match.score,
        "match_reasons": list(match.reasons),
        "functional_focus": list(workflow.functional_focus),
        "training_focus": list(workflow.training_focus),
        "required_inputs": [
            _required_input_to_dict(item) for item in scenario.required_inputs
        ],
        "expected_outputs": [
            _output_spec_to_dict(item) for item in scenario.expected_outputs
        ],
        "truth_requirements": list(scenario.truth_requirements),
        "material_requirements": list(scenario.material_requirements),
        "material_status": material_assessment.status,
        "material_provided_signals": list(material_assessment.provided_signals),
        "missing_required_inputs": [
            _required_input_to_dict(item)
            for item in material_assessment.missing_required_inputs
        ],
        "truth_status": (
            "needs_materials"
            if material_assessment.needs_followup
            else "ready_for_execution"
        ),
        "lifecycle_stage": (
            "intake" if material_assessment.needs_followup else "ready_for_execution"
        ),
        "next_actions": _next_actions(material_assessment),
        "test_task_directions": list(scenario.test_task_directions),
        "review_required_by_default": True,
    }
    if requester_meta:
        payload.update(requester_meta)
    return payload


def build_content_sop_workflow_payload(
    match: DepartmentWorkflowMatch,
    *,
    source: str,
    message_text: str,
    content_type: str,
    requester_meta: dict[str, Any] | None = None,
    knowledge_context: str = "",
    source_citations: list[dict[str, str]] | None = None,
    material_assessment: MaterialIntakeAssessment | None = None,
) -> dict[str, Any]:
    workflow = match.workflow
    scenario = match.scenario
    material_assessment = material_assessment or assess_material_intake(
        scenario,
        message_text,
    )
    lifecycle_stage = (
        "needs_materials"
        if material_assessment.needs_followup
        else "ready_for_generation"
    )
    payload: dict[str, Any] = {
        "source": source,
        "workflow_kind": "content_sop_workflow",
        "department": workflow.department_name,
        "department_id": workflow.department_id,
        "scenario_id": scenario.scenario_id,
        "scenario_name": scenario.name,
        "content_type": content_type,
        "brief": message_text.strip()[:500],
        "message_text": message_text,
        "knowledge_context": knowledge_context,
        "source_citations": source_citations or [],
        "creative_assumptions": [],
        "match_score": match.score,
        "match_reasons": list(match.reasons),
        "required_inputs": [
            _required_input_to_dict(item) for item in scenario.required_inputs
        ],
        "expected_outputs": [
            _output_spec_to_dict(item) for item in scenario.expected_outputs
        ],
        "truth_requirements": list(scenario.truth_requirements),
        "material_requirements": list(scenario.material_requirements),
        "material_status": material_assessment.status,
        "missing_required_inputs": [
            _required_input_to_dict(item)
            for item in material_assessment.missing_required_inputs
        ],
        "truth_status": (
            "needs_materials"
            if material_assessment.needs_followup
            else "ready_for_execution"
        ),
        "lifecycle_stage": lifecycle_stage,
        "next_actions": _content_sop_next_actions(material_assessment),
        "review_required_by_default": True,
        "generation_allowed": not material_assessment.needs_followup,
    }
    payload["communication_channel_policy"] = communication_channel_policy_for(
        department_id=workflow.department_id,
        message_text=message_text,
    )
    payload["quality_policy"] = build_content_sop_quality_policy()
    payload["quality_gate"] = evaluate_content_sop_payload(payload).to_dict()
    if requester_meta:
        payload.update(requester_meta)
    return payload


def _build_title(
    workflow: DepartmentWorkflow,
    scenario: Scenario,
    message_text: str,
) -> str:
    brief = message_text.strip().replace("\n", " ")[:48]
    prefix = f"部门工作流 | {workflow.department_name} | {scenario.name}"
    return f"{prefix} | {brief}" if brief else prefix


def _build_content_sop_title(
    workflow: DepartmentWorkflow,
    scenario: Scenario,
    message_text: str,
) -> str:
    brief = message_text.strip().replace("\n", " ")[:48]
    prefix = f"内容 SOP | {workflow.department_name} | {scenario.name}"
    return f"{prefix} | {brief}" if brief else prefix


def _required_input_to_dict(item: RequiredInput) -> dict[str, Any]:
    return {
        "key": item.key,
        "label": item.label,
        "description": item.description,
        "required": item.required,
        "examples": list(item.examples),
    }


def _output_spec_to_dict(item: OutputSpec) -> dict[str, Any]:
    return {
        "key": item.key,
        "label": item.label,
        "description": item.description,
        "format_hint": item.format_hint,
    }


def _next_actions(assessment: MaterialIntakeAssessment) -> list[str]:
    if assessment.status == "ready":
        return ["execute_workflow", "produce_expected_outputs", "record_result"]
    labels = [item.label for item in assessment.missing_required_inputs]
    if assessment.status == "partial":
        return [
            "confirm_missing_materials",
            "continue_with_available_context_if_approved",
            "record_pending_inputs",
            *[f"collect:{label}" for label in labels],
        ]
    return [
        "request_materials",
        "pause_before_fact_based_output",
        "record_pending_inputs",
        *[f"collect:{label}" for label in labels],
    ]


def _content_sop_next_actions(assessment: MaterialIntakeAssessment) -> list[str]:
    labels = [item.label for item in assessment.missing_required_inputs]
    if not assessment.needs_followup:
        return [
            "retrieve_knowledge_context",
            "build_creative_brief",
            "produce_copy_image_prompt_video_script",
            "record_source_citations",
            "request_employee_review",
        ]
    return [
        "send_material_intake_card",
        "pause_before_generation",
        "record_pending_inputs",
        *[f"collect:{label}" for label in labels],
    ]
