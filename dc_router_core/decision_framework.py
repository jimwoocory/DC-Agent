"""Router Decision Framework v1.

This module is deliberately observation-first. It records how a message looks
to the router before the legacy intent resolver maps it to a provider. LLM
labels may be carried as candidate evidence, but deterministic rule gates make
the final safety decisions.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from json import loads
from pathlib import Path
from typing import Any


class ObservationValue(StrEnum):
    UNKNOWN = "unknown"
    EMPLOYEE_CHAT = "employee_chat"
    BUSINESS_CONTEXT = "business_context"
    FOLLOWUP = "followup"
    NEW_REQUEST = "new_request"
    SOURCE_OBJECT = "source_object"
    GENERATED_OBJECT = "generated_object"
    IMPLICIT = "implicit"
    EXPLICIT_EXECUTE = "explicit_execute"
    DIRECT_MEDIA = "direct_media"
    DEPARTMENT_CONTEXT = "department_context"
    RESOURCE_REQUIRED = "resource_required"
    RESOURCE_NOT_REQUIRED = "resource_not_required"
    ALLOW = "allow"
    BLOCK_QUEUE = "block_queue"
    ANSWER = "answer"
    PREPROCESS_MEDIA = "preprocess_media"
    ASK_CLARIFY = "ask_clarify"


class CandidateSource(StrEnum):
    LLM = "llm"
    RULE = "rule"
    METADATA = "metadata"
    REPLAY = "replay"


@dataclass(frozen=True, slots=True)
class CandidateEvidence:
    """Non-authoritative label/evidence from an LLM or another observer."""

    source: CandidateSource
    label: str
    evidence: str = ""
    confidence: float | None = None


@dataclass(frozen=True, slots=True)
class RouterObservation:
    speaker_context: ObservationValue = ObservationValue.UNKNOWN
    conversation_state: ObservationValue = ObservationValue.NEW_REQUEST
    object_reference: ObservationValue = ObservationValue.UNKNOWN
    action_force: ObservationValue = ObservationValue.IMPLICIT
    department_signal: ObservationValue = ObservationValue.UNKNOWN
    resource_need: ObservationValue = ObservationValue.RESOURCE_NOT_REQUIRED
    risk_gate: ObservationValue = ObservationValue.ALLOW
    next_action: ObservationValue = ObservationValue.ANSWER
    candidates: tuple[CandidateEvidence, ...] = ()
    evidence: tuple[str, ...] = ()

    def to_metadata(self) -> dict[str, str]:
        metadata = {
            "rdf_version": "1",
            "rdf_speaker_context": self.speaker_context.value,
            "rdf_conversation_state": self.conversation_state.value,
            "rdf_object_reference": self.object_reference.value,
            "rdf_action_force": self.action_force.value,
            "rdf_department_signal": self.department_signal.value,
            "rdf_resource_need": self.resource_need.value,
            "rdf_risk_gate": self.risk_gate.value,
            "rdf_next_action": self.next_action.value,
        }
        if self.evidence:
            metadata["rdf_evidence"] = "|".join(self.evidence)
        if self.candidates:
            metadata["rdf_candidate_sources"] = ",".join(
                f"{candidate.source.value}:{candidate.label}"
                for candidate in self.candidates
            )
        return metadata


@dataclass(frozen=True, slots=True)
class RuleGateDecision:
    queue_allowed: bool
    queue_reason: str = ""
    llm_can_execute: bool = False
    notes: tuple[str, ...] = ()

    def to_metadata(self) -> dict[str, str]:
        metadata = {
            "rule_gate_version": "1",
            "queue_allowed": "true" if self.queue_allowed else "false",
            "llm_can_execute": "true" if self.llm_can_execute else "false",
        }
        if self.queue_reason:
            metadata["queue_reason"] = self.queue_reason
        if self.notes:
            metadata["rule_gate_notes"] = "|".join(self.notes)
        return metadata


@dataclass(frozen=True, slots=True)
class RouterDecisionContext:
    observation: RouterObservation
    gate: RuleGateDecision

    def to_metadata(self) -> dict[str, str]:
        metadata = self.observation.to_metadata()
        metadata.update(self.gate.to_metadata())
        return metadata


@dataclass(frozen=True, slots=True)
class ReplaySample:
    sample_id: str
    text: str
    metadata: Mapping[str, str] = field(default_factory=dict)
    attachment_kinds: tuple[str, ...] = ()
    attachment_summary: str | None = None
    candidates: tuple[CandidateEvidence, ...] = ()
    expected_slots: Mapping[str, str] = field(default_factory=dict)
    expected_rule_gate: Mapping[str, Any] = field(default_factory=dict)
    expected_final: Mapping[str, Any] = field(default_factory=dict)


def replay_sample_from_mapping(raw: Mapping[str, Any]) -> ReplaySample:
    sample_id = str(raw.get("sample_id") or "").strip()
    text = str(raw.get("text") or "")
    if not sample_id:
        raise ValueError("Replay sample missing sample_id")
    if not text:
        raise ValueError(f"Replay sample {sample_id!r} missing text")

    raw_metadata = raw.get("metadata") or {}
    if not isinstance(raw_metadata, Mapping):
        raise TypeError(f"Replay sample {sample_id!r} metadata must be an object")
    metadata = {str(key): str(value) for key, value in raw_metadata.items()}

    raw_attachment_kinds = raw.get("attachment_kinds") or ()
    if not isinstance(raw_attachment_kinds, list | tuple):
        raise TypeError(f"Replay sample {sample_id!r} attachment_kinds must be a list")
    attachment_kinds = tuple(str(kind) for kind in raw_attachment_kinds)

    raw_candidates = raw.get("candidates") or ()
    if not isinstance(raw_candidates, list | tuple):
        raise TypeError(f"Replay sample {sample_id!r} candidates must be a list")
    candidates = tuple(
        _candidate_from_mapping(sample_id, candidate) for candidate in raw_candidates
    )

    raw_summary = raw.get("attachment_summary")
    expected_slots = _string_mapping(
        raw.get("expected_slots") or {},
        sample_id=sample_id,
        field_name="expected_slots",
    )
    expected_rule_gate = _loose_mapping(
        raw.get("expected_rule_gate") or {},
        sample_id=sample_id,
        field_name="expected_rule_gate",
    )
    expected_final = _loose_mapping(
        raw.get("expected_final") or {},
        sample_id=sample_id,
        field_name="expected_final",
    )
    return ReplaySample(
        sample_id=sample_id,
        text=text,
        metadata=metadata,
        attachment_kinds=attachment_kinds,
        attachment_summary=str(raw_summary) if raw_summary is not None else None,
        candidates=candidates,
        expected_slots=expected_slots,
        expected_rule_gate=expected_rule_gate,
        expected_final=expected_final,
    )


def load_replay_samples(path: str | Path) -> tuple[ReplaySample, ...]:
    payload = loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise TypeError("Router replay file must contain a JSON array")
    samples = tuple(replay_sample_from_mapping(item) for item in payload)
    sample_ids = [sample.sample_id for sample in samples]
    duplicates = sorted(
        sample_id for sample_id in set(sample_ids) if sample_ids.count(sample_id) > 1
    )
    if duplicates:
        raise ValueError(f"Duplicate replay sample_id(s): {', '.join(duplicates)}")
    return samples


def _candidate_from_mapping(sample_id: str, raw: Any) -> CandidateEvidence:
    if not isinstance(raw, Mapping):
        raise TypeError(f"Replay sample {sample_id!r} candidate must be an object")
    source = CandidateSource(str(raw.get("source") or CandidateSource.REPLAY.value))
    label = str(raw.get("label") or "").strip()
    if not label:
        raise ValueError(f"Replay sample {sample_id!r} candidate missing label")
    raw_confidence = raw.get("confidence")
    confidence = float(raw_confidence) if raw_confidence is not None else None
    return CandidateEvidence(
        source=source,
        label=label,
        evidence=str(raw.get("evidence") or ""),
        confidence=confidence,
    )


def _string_mapping(
    raw: Any,
    *,
    sample_id: str,
    field_name: str,
) -> dict[str, str]:
    mapping = _loose_mapping(raw, sample_id=sample_id, field_name=field_name)
    return {str(key): str(value) for key, value in mapping.items()}


def _loose_mapping(
    raw: Any,
    *,
    sample_id: str,
    field_name: str,
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise TypeError(f"Replay sample {sample_id!r} {field_name} must be an object")
    return {str(key): value for key, value in raw.items()}


_FOLLOWUP_RE = re.compile(
    r"(不是很理想|不太理想|能不能再|再精细|再细|毛边|边缘|发丝|头发部分|"
    r"继续优化|再调整|还有点|上一张|刚才那张|这个结果)",
    re.IGNORECASE,
)
_EXPLICIT_EXECUTE_RE = re.compile(
    r"(执行|开始|直接|立刻|马上|帮我)(?:.{0,12})(精修|生成|生图|排队|跑一下|处理)",
    re.IGNORECASE,
)
_MEDIA_RE = re.compile(
    r"(图片|图|海报|封面|长图|生图|生成一张|即梦|dreamina|抠图|去背景|透明PNG|透明 png)",
    re.IGNORECASE,
)
_DEPARTMENT_RE = re.compile(
    r"(客户部|策略部|策划部|中台|活动统筹部|设计部|影视制作部|AI应用部|"
    r"综合部|财务部|品宣|品宣部|柳汽|planning|client_dept|execution_ops|"
    r"design_dept|film_production|ai_application|brand_publicity)",
    re.IGNORECASE,
)
_QUEUE_RESOURCE_RE = re.compile(
    r"(排队|深度任务|Hermes|本地CLI|本地 CLI|资源|quota|配额)",
    re.IGNORECASE,
)


def build_decision_context(
    text: str,
    *,
    metadata: Mapping[str, str] | None = None,
    attachment_kinds: tuple[str, ...] = (),
    attachment_summary: str | None = None,
    candidates: tuple[CandidateEvidence, ...] = (),
) -> RouterDecisionContext:
    """Build observation slots and deterministic gate metadata for a message."""

    metadata = metadata or {}
    evidence: list[str] = []
    text = text or ""
    combined = "\n".join(
        bit
        for bit in (
            text,
            attachment_summary or "",
            metadata.get("department", ""),
            metadata.get("requester_department", ""),
            metadata.get("requester_department_path", ""),
        )
        if bit
    )

    speaker_context = (
        ObservationValue.EMPLOYEE_CHAT
        if metadata.get("platform_id") or metadata.get("router_mode") == "business"
        else ObservationValue.UNKNOWN
    )
    if speaker_context is ObservationValue.EMPLOYEE_CHAT:
        evidence.append("platform_context")

    has_attachment = bool(attachment_kinds)
    has_media = has_attachment or bool(_MEDIA_RE.search(combined))
    is_followup = bool(_FOLLOWUP_RE.search(text))
    explicit_execute = bool(_EXPLICIT_EXECUTE_RE.search(text))
    department_signal = bool(_DEPARTMENT_RE.search(combined))

    observation = RouterObservation(
        speaker_context=speaker_context,
        conversation_state=(
            ObservationValue.FOLLOWUP if is_followup else ObservationValue.NEW_REQUEST
        ),
        object_reference=(
            ObservationValue.SOURCE_OBJECT
            if has_attachment or "抠图" in text or "去背景" in text
            else (
                ObservationValue.GENERATED_OBJECT
                if is_followup
                else ObservationValue.UNKNOWN
            )
        ),
        action_force=(
            ObservationValue.EXPLICIT_EXECUTE
            if explicit_execute
            else ObservationValue.IMPLICIT
        ),
        department_signal=(
            ObservationValue.DEPARTMENT_CONTEXT
            if department_signal
            else ObservationValue.UNKNOWN
        ),
        resource_need=(
            ObservationValue.RESOURCE_REQUIRED
            if _QUEUE_RESOURCE_RE.search(combined) and explicit_execute
            else ObservationValue.RESOURCE_NOT_REQUIRED
        ),
        risk_gate=(
            ObservationValue.BLOCK_QUEUE
            if is_followup and not explicit_execute
            else ObservationValue.ALLOW
        ),
        next_action=(
            ObservationValue.PREPROCESS_MEDIA
            if has_attachment or has_media
            else (
                ObservationValue.ASK_CLARIFY
                if is_followup and not explicit_execute
                else ObservationValue.ANSWER
            )
        ),
        candidates=candidates,
        evidence=tuple(evidence),
    )
    gate = _build_rule_gate(observation)
    return RouterDecisionContext(observation=observation, gate=gate)


def _build_rule_gate(observation: RouterObservation) -> RuleGateDecision:
    notes: list[str] = []
    if observation.candidates:
        notes.append("candidate_only_no_execution")
    if observation.department_signal is ObservationValue.DEPARTMENT_CONTEXT:
        notes.append("department_context_not_entrypoint")
    if observation.next_action is ObservationValue.PREPROCESS_MEDIA:
        notes.append("media_before_department")
    if observation.risk_gate is ObservationValue.BLOCK_QUEUE:
        notes.append("feedback_or_followup_blocks_queue")
        return RuleGateDecision(
            queue_allowed=False,
            llm_can_execute=False,
            notes=tuple(notes),
        )
    if observation.resource_need is ObservationValue.RESOURCE_REQUIRED:
        return RuleGateDecision(
            queue_allowed=True,
            queue_reason="explicit_resource_request",
            llm_can_execute=False,
            notes=tuple(notes),
        )
    notes.append("no_real_queue_reason")
    return RuleGateDecision(
        queue_allowed=False,
        llm_can_execute=False,
        notes=tuple(notes),
    )


__all__ = [
    "CandidateEvidence",
    "CandidateSource",
    "ObservationValue",
    "ReplaySample",
    "RouterDecisionContext",
    "RouterObservation",
    "RuleGateDecision",
    "build_decision_context",
    "load_replay_samples",
    "replay_sample_from_mapping",
]
