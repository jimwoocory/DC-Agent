"""Offline LLM annotation contract for Router Decision Framework.

The parser in this module intentionally returns candidate evidence only. It is
not part of live routing and it rejects any model output that tries to decide
tools, providers, queues, or final routes.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from dc_router_core.decision_framework import CandidateEvidence, CandidateSource

ANNOTATION_SCHEMA_VERSION = 1
DEFAULT_ROUTER_LABEL_MODEL = "aihubmix/qwen3.7-max"
OFFLINE_REVIEW_MODELS = frozenset(
    {
        DEFAULT_ROUTER_LABEL_MODEL,
        "aihubmix/claude-sonnet-4-6",
        "aihubmix/claude-opus-4-7",
        "aihubmix/claude-opus-4-8",
    }
)
REJECTED_ROUTER_LABEL_MODELS = frozenset(
    {
        "aihubmix/doubao-seed-2-1-pro",
        "doubao-seed-2.1pro",
        "doubao-seed-2-1-pro",
    }
)
FORBIDDEN_DECISION_FIELDS = frozenset(
    {
        "tool_execute",
        "queue",
        "queue_allowed",
        "queue_reason",
        "provider_id",
        "target_model",
        "intent",
        "route",
        "department_workflow",
        "task_create",
    }
)


def parse_llm_annotation(
    payload: str | Mapping[str, Any],
) -> tuple[CandidateEvidence, ...]:
    """Parse an offline LLM annotation into non-authoritative candidates."""

    raw = json.loads(payload) if isinstance(payload, str) else dict(payload)
    if not isinstance(raw, Mapping):
        raise TypeError("Router LLM annotation must be a JSON object")
    _reject_forbidden_decision_fields(raw)

    version = int(raw.get("schema_version") or ANNOTATION_SCHEMA_VERSION)
    if version != ANNOTATION_SCHEMA_VERSION:
        raise ValueError(f"Unsupported router LLM annotation schema_version: {version}")

    model_id = str(raw.get("model_id") or DEFAULT_ROUTER_LABEL_MODEL)
    if model_id in REJECTED_ROUTER_LABEL_MODELS:
        raise ValueError(f"Model is not allowed for router annotation: {model_id}")
    if model_id and model_id not in OFFLINE_REVIEW_MODELS:
        raise ValueError(f"Unknown router annotation model_id: {model_id}")

    raw_candidates = raw.get("candidates") or ()
    if not isinstance(raw_candidates, list | tuple):
        raise TypeError("Router LLM annotation candidates must be a list")
    return tuple(_candidate_from_annotation(item) for item in raw_candidates)


def build_annotation_prompt_contract() -> dict[str, Any]:
    """Return the machine-readable prompt boundary for offline annotators."""

    return {
        "schema_version": ANNOTATION_SCHEMA_VERSION,
        "default_model_id": DEFAULT_ROUTER_LABEL_MODEL,
        "allowed_output": {
            "model_id": "string",
            "candidates": [
                {
                    "label": "short candidate label",
                    "evidence": "short evidence summary",
                    "confidence": "0.0-1.0",
                }
            ],
        },
        "forbidden_fields": sorted(FORBIDDEN_DECISION_FIELDS),
        "decision_policy": "candidate_evidence_only_never_execute_or_queue",
    }


def _candidate_from_annotation(raw: Any) -> CandidateEvidence:
    if not isinstance(raw, Mapping):
        raise TypeError("Router LLM annotation candidate must be an object")
    _reject_forbidden_decision_fields(raw)
    label = str(raw.get("label") or "").strip()
    if not label:
        raise ValueError("Router LLM annotation candidate missing label")
    evidence = str(raw.get("evidence") or "").strip()
    confidence = _confidence(raw.get("confidence"))
    return CandidateEvidence(
        source=CandidateSource.LLM,
        label=label,
        evidence=evidence,
        confidence=confidence,
    )


def _confidence(value: Any) -> float | None:
    if value is None:
        return None
    confidence = float(value)
    if confidence < 0.0 or confidence > 1.0:
        raise ValueError("Router LLM annotation confidence must be between 0 and 1")
    return confidence


def _reject_forbidden_decision_fields(value: Mapping[str, Any]) -> None:
    found = sorted(FORBIDDEN_DECISION_FIELDS.intersection(value))
    if found:
        raise ValueError(
            "Router LLM annotation attempted final decision fields: " + ", ".join(found)
        )


__all__ = [
    "ANNOTATION_SCHEMA_VERSION",
    "DEFAULT_ROUTER_LABEL_MODEL",
    "FORBIDDEN_DECISION_FIELDS",
    "OFFLINE_REVIEW_MODELS",
    "REJECTED_ROUTER_LABEL_MODELS",
    "build_annotation_prompt_contract",
    "parse_llm_annotation",
]
