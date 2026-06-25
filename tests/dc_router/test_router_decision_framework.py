from __future__ import annotations

import json
from pathlib import Path

import pytest

from dc_router_core.decision_framework import (
    CandidateEvidence,
    CandidateSource,
    ObservationValue,
    build_decision_context,
    load_replay_samples,
    replay_sample_from_mapping,
)
from dc_router_core.entrypoint import DCRouter, MessageEnvelope
from dc_router_core.llm_annotation import (
    DEFAULT_ROUTER_LABEL_MODEL,
    build_annotation_prompt_contract,
    parse_llm_annotation,
)

CONTRACT_PATH = Path("harness/contracts/router_decision_framework.json")
REQUIRED_METADATA_KEYS = {
    "rdf_speaker_context",
    "rdf_conversation_state",
    "rdf_object_reference",
    "rdf_action_force",
    "rdf_department_signal",
    "rdf_resource_need",
    "rdf_risk_gate",
    "rdf_next_action",
}


def _contract() -> dict:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def _replay_samples():
    return load_replay_samples(_contract()["replay_dataset"])


def test_observation_slots_are_contract_metadata() -> None:
    context = build_decision_context(
        "帮我直接生成一张缤果夏季主视觉海报",
        metadata={"platform_id": "巅池-Agent小助手", "department": "planning"},
    )

    metadata = context.to_metadata()

    assert REQUIRED_METADATA_KEYS <= set(metadata)
    assert metadata["rdf_version"] == "1"
    assert metadata["rdf_speaker_context"] == ObservationValue.EMPLOYEE_CHAT.value
    assert metadata["rdf_department_signal"] == (
        ObservationValue.DEPARTMENT_CONTEXT.value
    )
    assert metadata["rdf_next_action"] == ObservationValue.PREPROCESS_MEDIA.value
    assert "media_before_department" in metadata["rule_gate_notes"]


def test_llm_candidate_cannot_execute_or_queue_by_itself() -> None:
    context = build_decision_context(
        "刚才那个结果还有点问题，先别排队，我问一下原因",
        metadata={"platform_id": "巅池-Agent小助手"},
        candidates=(
            CandidateEvidence(
                source=CandidateSource.LLM,
                label="queue_candidate",
                evidence="ambiguous follow-up",
                confidence=0.72,
            ),
        ),
    )

    metadata = context.to_metadata()

    assert metadata["queue_allowed"] == "false"
    assert metadata["llm_can_execute"] == "false"
    assert "candidate_only_no_execution" in metadata["rule_gate_notes"]
    assert metadata["rdf_candidate_sources"] == "llm:queue_candidate"


def test_offline_llm_annotation_parses_candidate_evidence_only() -> None:
    candidates = parse_llm_annotation(
        {
            "schema_version": 1,
            "model_id": DEFAULT_ROUTER_LABEL_MODEL,
            "candidates": [
                {
                    "label": "queue_candidate",
                    "evidence": "ambiguous follow-up with no explicit execute",
                    "confidence": 0.72,
                }
            ],
        }
    )

    context = build_decision_context(
        "刚才那个结果还有点问题，先别排队，我问一下原因",
        metadata={"platform_id": "巅池-Agent小助手"},
        candidates=candidates,
    )
    metadata = context.to_metadata()

    assert candidates[0].source is CandidateSource.LLM
    assert metadata["rdf_candidate_sources"] == "llm:queue_candidate"
    assert metadata["llm_can_execute"] == "false"
    assert metadata["queue_allowed"] == "false"
    assert "candidate_only_no_execution" in metadata["rule_gate_notes"]


@pytest.mark.parametrize("field", ["tool_execute", "queue", "provider_id"])
def test_offline_llm_annotation_rejects_final_decision_fields(field) -> None:
    with pytest.raises(ValueError, match="final decision fields"):
        parse_llm_annotation(
            {
                "schema_version": 1,
                "model_id": DEFAULT_ROUTER_LABEL_MODEL,
                field: "not_allowed",
                "candidates": [{"label": "queue_candidate"}],
            }
        )


def test_offline_llm_annotation_rejects_doubao_router_labeler() -> None:
    with pytest.raises(ValueError, match="not allowed"):
        parse_llm_annotation(
            {
                "schema_version": 1,
                "model_id": "aihubmix/doubao-seed-2-1-pro",
                "candidates": [{"label": "queue_candidate"}],
            }
        )


def test_offline_llm_annotation_prompt_contract_forbids_execution_fields() -> None:
    contract = build_annotation_prompt_contract()

    assert contract["default_model_id"] == DEFAULT_ROUTER_LABEL_MODEL
    assert (
        contract["decision_policy"] == "candidate_evidence_only_never_execute_or_queue"
    )
    assert "queue" in contract["forbidden_fields"]
    assert "provider_id" in contract["forbidden_fields"]


def test_replay_samples_cover_contract_required_cases() -> None:
    contract_cases = set(_contract()["replay_coverage"])
    sample_cases = {sample.sample_id for sample in _replay_samples()}

    assert contract_cases <= sample_cases


def test_contract_replay_dataset_is_loadable() -> None:
    samples = _replay_samples()

    assert samples
    assert all(sample.sample_id and sample.text for sample in samples)


def test_replay_loader_parses_llm_candidates() -> None:
    sample = replay_sample_from_mapping(
        {
            "sample_id": "candidate_case",
            "text": "这个是不是要排队？",
            "candidates": [
                {
                    "source": "llm",
                    "label": "queue_candidate",
                    "confidence": 0.61,
                }
            ],
        }
    )

    assert sample.candidates[0].source is CandidateSource.LLM
    assert sample.candidates[0].label == "queue_candidate"
    assert sample.candidates[0].confidence == 0.61


def test_replay_loader_parses_expected_contracts() -> None:
    sample = replay_sample_from_mapping(
        {
            "sample_id": "expected_case",
            "text": "品宣和中台策略部都提到直播账号运营，这个先怎么分工？",
            "expected_slots": {"department_signal": "department_context"},
            "expected_rule_gate": {
                "queue_allowed": "false",
                "rule_gate_notes_contains": ["department_context_not_entrypoint"],
            },
            "expected_final": {
                "source_not": "department_workflow",
                "metadata_absent": ["department_workflow"],
                "metadata_equals": {"queue_allowed": "false"},
            },
        }
    )

    assert sample.expected_slots["department_signal"] == "department_context"
    assert sample.expected_rule_gate["queue_allowed"] == "false"
    assert sample.expected_final["source_not"] == "department_workflow"
    assert sample.expected_final["metadata_equals"]["queue_allowed"] == "false"


def test_replay_loader_rejects_duplicate_ids(tmp_path) -> None:
    replay_file = tmp_path / "duplicates.json"
    replay_file.write_text(
        json.dumps(
            [
                {"sample_id": "dup", "text": "你好"},
                {"sample_id": "dup", "text": "再问一下"},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Duplicate replay sample_id"):
        load_replay_samples(replay_file)


@pytest.mark.parametrize("sample", _replay_samples(), ids=lambda s: s.sample_id)
def test_replay_samples_have_expected_gates(sample) -> None:
    context = build_decision_context(
        sample.text,
        metadata=sample.metadata,
        attachment_kinds=sample.attachment_kinds,
        attachment_summary=sample.attachment_summary,
        candidates=sample.candidates,
    )
    metadata = context.to_metadata()

    assert REQUIRED_METADATA_KEYS <= set(metadata)
    assert metadata["llm_can_execute"] == "false"
    if "followup" in sample.sample_id or "false_queue" in sample.sample_id:
        assert metadata["queue_allowed"] == "false"
        assert metadata["rdf_risk_gate"] == ObservationValue.BLOCK_QUEUE.value
    if sample.sample_id == "direct_image_generation_media_first":
        assert metadata["rdf_next_action"] == ObservationValue.PREPROCESS_MEDIA.value
        assert "media_before_department" in metadata["rule_gate_notes"]
    if "department" in sample.sample_id or "liuqi" in sample.sample_id:
        assert metadata["rdf_department_signal"] == (
            ObservationValue.DEPARTMENT_CONTEXT.value
        )
    for slot, expected in sample.expected_slots.items():
        assert metadata[f"rdf_{slot}"] == expected
    for key, expected in sample.expected_rule_gate.items():
        if key == "rule_gate_notes_contains":
            notes = metadata.get("rule_gate_notes", "")
            assert all(str(note) in notes for note in expected)
        else:
            assert metadata[key] == str(expected)


def test_approved_replay_samples_define_executable_expectations() -> None:
    for sample in _replay_samples():
        assert sample.expected_slots, sample.sample_id
        assert sample.expected_rule_gate, sample.sample_id
        assert sample.expected_final, sample.sample_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sample",
    [sample for sample in _replay_samples() if sample.expected_final],
    ids=lambda sample: sample.sample_id,
)
async def test_replay_samples_lock_expected_final_router_behavior(sample) -> None:
    router = DCRouter()

    decision = await router.decide(
        MessageEnvelope(text=sample.text, metadata=dict(sample.metadata))
    )

    source_not = sample.expected_final.get("source_not")
    if source_not:
        assert decision.source != source_not
    expected_source = sample.expected_final.get("source")
    if expected_source:
        assert decision.source == expected_source
    expected_intent = sample.expected_final.get("intent")
    if expected_intent:
        assert decision.intent == expected_intent
    expected_provider_id = sample.expected_final.get("provider_id")
    if expected_provider_id:
        assert decision.provider_id == expected_provider_id
    expected_action = sample.expected_final.get("action")
    if expected_action:
        assert decision.action == expected_action
    expected_depth = sample.expected_final.get("depth")
    if expected_depth:
        assert decision.depth == expected_depth
    for metadata_key in sample.expected_final.get("metadata_absent", ()):
        assert metadata_key not in decision.metadata
    for metadata_key in sample.expected_final.get("metadata_present", ()):
        assert metadata_key in decision.metadata
    for metadata_key, expected in sample.expected_final.get(
        "metadata_equals", {}
    ).items():
        assert decision.metadata.get(metadata_key) == str(expected)


@pytest.mark.asyncio
async def test_dc_router_serializes_framework_metadata() -> None:
    router = DCRouter()

    decision = await router.decide(
        MessageEnvelope(
            text="你好，我想问一下今天这个事情怎么处理",
            metadata={"platform_id": "巅池-Agent小助手"},
        )
    )

    assert REQUIRED_METADATA_KEYS <= set(decision.metadata)
    assert decision.metadata["queue_allowed"] == "false"
    assert decision.metadata["llm_can_execute"] == "false"
