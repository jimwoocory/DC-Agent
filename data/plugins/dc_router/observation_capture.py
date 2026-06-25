"""Observe-only router decision capture and replay proposal helpers."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from dc_router_core.legacy_surface import LegacySurfaceRisk, legacy_surfaces

from .paths import data_path

DEFAULT_OBSERVATION_PATH = data_path("runtime", "router_decision_observations.jsonl")
PROPOSAL_SCHEMA_VERSION = 1
RECORD_SCHEMA_VERSION = 1
REVIEW_PACK_SCHEMA_VERSION = 1
APPROVED_REPLAY_STATUS = "approved"

_OBSERVED_SLOT_KEYS: tuple[str, ...] = (
    "rdf_speaker_context",
    "rdf_conversation_state",
    "rdf_object_reference",
    "rdf_action_force",
    "rdf_department_signal",
    "rdf_resource_need",
    "rdf_risk_gate",
    "rdf_next_action",
)
_RULE_GATE_KEYS: tuple[str, ...] = (
    "rule_gate_version",
    "queue_allowed",
    "queue_reason",
    "llm_can_execute",
    "rule_gate_notes",
)
_RDF_SLOT_PREFIX = "rdf_"
_REPLAY_REVIEW_ACTIONS: tuple[str, ...] = (
    "human_reconstruct_message",
    "verify_expected_slots",
    "add_to_replay_dataset_if_approved",
)


def build_observation_record(
    *,
    event: Any,
    envelope: Any,
    decision: Any,
    handled: bool,
    dry_run: bool,
) -> dict[str, Any]:
    """Build a redacted observe-only record for offline router review."""

    metadata = _metadata(decision)
    text = str(getattr(envelope, "text", "") or "")
    platform_id = str(
        metadata.get("platform_id") or _safe_call(event, "get_platform_id")
    )
    user_id = str(
        getattr(envelope, "user_id", "") or _safe_call(event, "get_sender_id")
    )
    session_id = str(
        getattr(envelope, "session_id", "") or getattr(event, "unified_msg_origin", "")
    )
    disabled_legacy_cli = _event_extra(event, "dc_router_disabled_legacy_cli")
    disabled_legacy_cli_card = _event_extra(
        event, "dc_router_disabled_legacy_cli_card_job_id"
    )

    return {
        "schema_version": RECORD_SCHEMA_VERSION,
        "observed_at": datetime.now(UTC).isoformat(),
        "text_hash": _hash_text(text),
        "text_shape": _text_shape(text),
        "platform_id": platform_id,
        "user_hash": _hash_identity(user_id),
        "session_hash": _hash_identity(session_id),
        "slots": {
            key: str(metadata[key]) for key in _OBSERVED_SLOT_KEYS if key in metadata
        },
        "rule_gate": {
            key: str(metadata[key]) for key in _RULE_GATE_KEYS if key in metadata
        },
        "candidate_sources": str(metadata.get("rdf_candidate_sources", "")),
        "final": {
            "intent": str(getattr(decision, "intent", "") or ""),
            "provider_id": str(getattr(decision, "provider_id", "") or ""),
            "source": str(getattr(decision, "source", "") or ""),
            "depth": str(getattr(decision, "depth", "") or ""),
            "action": str(getattr(decision, "action", "") or ""),
            "handled": bool(handled),
            "dry_run": bool(dry_run),
        },
        "side_effects": {
            "disabled_legacy_cli": bool(disabled_legacy_cli),
            "disabled_legacy_cli_card": bool(disabled_legacy_cli_card),
        },
    }


def capture_router_decision_observation(
    *,
    event: Any,
    envelope: Any,
    decision: Any,
    handled: bool,
    dry_run: bool,
    path: Path | None = None,
) -> Path:
    record = build_observation_record(
        event=event,
        envelope=envelope,
        decision=decision,
        handled=handled,
        dry_run=dry_run,
    )
    target = path or DEFAULT_OBSERVATION_PATH
    append_observation_record(record, target)
    return target


def append_observation_record(record: Mapping[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
        fh.write("\n")


def load_observation_records(path: Path) -> tuple[dict[str, Any], ...]:
    if not path.exists():
        return ()
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if not stripped:
                continue
            item = json.loads(stripped)
            if isinstance(item, dict):
                records.append(item)
    return tuple(records)


def build_replay_proposals(
    records: Iterable[Mapping[str, Any]],
    *,
    limit: int = 50,
) -> list[dict[str, Any]]:
    proposals: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        reason = _proposal_reason(record)
        if reason is None:
            continue
        text_hash = str(record.get("text_hash") or "")
        proposal_id = f"{reason}:{text_hash}"
        if proposal_id in seen:
            continue
        seen.add(proposal_id)
        proposals.append(
            {
                "schema_version": PROPOSAL_SCHEMA_VERSION,
                "proposal_id": proposal_id,
                "status": "review_required",
                "reason": reason,
                "text_hash": text_hash,
                "text_shape": dict(record.get("text_shape") or {}),
                "observed_slots": dict(record.get("slots") or {}),
                "observed_rule_gate": dict(record.get("rule_gate") or {}),
                "candidate_sources": str(record.get("candidate_sources") or ""),
                "final": dict(record.get("final") or {}),
                "side_effects": dict(record.get("side_effects") or {}),
            }
        )
        proposals[-1]["replay_candidate"] = build_replay_review_candidate(proposals[-1])
        if len(proposals) >= limit:
            break
    return proposals


def build_replay_review_candidate(
    proposal: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the sanitized shape a human can promote into replay data."""

    proposal_id = str(proposal.get("proposal_id") or "")
    text_hash = str(proposal.get("text_hash") or "")
    final = proposal.get("final") if isinstance(proposal.get("final"), Mapping) else {}
    side_effects = (
        proposal.get("side_effects")
        if isinstance(proposal.get("side_effects"), Mapping)
        else {}
    )

    return {
        "status": "review_required",
        "sample_id_suggestion": _sample_id_suggestion(
            str(proposal.get("reason") or "router_review"), text_hash
        ),
        "source_observation_hash": text_hash,
        "source_proposal_id": proposal_id,
        "input_text_policy": "human_reconstruct_from_private_context",
        "message_text": None,
        "expected_slots": _normalized_slots(proposal.get("observed_slots")),
        "expected_rule_gate": dict(proposal.get("observed_rule_gate") or {}),
        "expected_final": {
            "intent": str(final.get("intent") or ""),
            "source": str(final.get("source") or ""),
            "provider_id": str(final.get("provider_id") or ""),
        },
        "expected_side_effects": {
            "disabled_legacy_cli": bool(side_effects.get("disabled_legacy_cli")),
            "disabled_legacy_cli_card": bool(
                side_effects.get("disabled_legacy_cli_card")
            ),
        },
        "review_actions": list(_REPLAY_REVIEW_ACTIONS),
    }


def write_replay_proposals(
    *,
    observations_path: Path,
    output_path: Path,
    limit: int = 50,
) -> Path:
    proposals = build_replay_proposals(
        load_observation_records(observations_path),
        limit=limit,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(proposals, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return output_path


def build_review_pack(
    records: Iterable[Mapping[str, Any]],
    *,
    limit: int = 50,
) -> dict[str, Any]:
    """Build a redacted daily review pack from router observations."""

    records_tuple = tuple(records)
    proposals = build_replay_proposals(records_tuple, limit=limit)
    reasons = _count_by_key(proposals, "reason")
    final_intents = _count_nested(proposals, "final", "intent")
    final_sources = _count_nested(proposals, "final", "source")
    side_effects = {
        "disabled_legacy_cli": sum(
            1
            for item in proposals
            if item.get("side_effects", {}).get("disabled_legacy_cli")
        ),
        "disabled_legacy_cli_card": sum(
            1
            for item in proposals
            if item.get("side_effects", {}).get("disabled_legacy_cli_card")
        ),
    }
    return {
        "schema_version": REVIEW_PACK_SCHEMA_VERSION,
        "status": "review_required",
        "generated_at": datetime.now(UTC).isoformat(),
        "input_observation_count": len(records_tuple),
        "proposal_count": len(proposals),
        "reason_counts": reasons,
        "final_intent_counts": final_intents,
        "final_source_counts": final_sources,
        "side_effect_counts": side_effects,
        "review_policy": "manual_review_only_no_router_mutation",
        "promotion_policy": "approve_candidate_then_use_router_promote_replay_candidate",
        "legacy_surface_priorities": _legacy_surface_priorities(),
        "candidates": proposals,
    }


def write_review_pack(
    *,
    observations_path: Path,
    output_path: Path,
    limit: int = 50,
) -> Path:
    review_pack = build_review_pack(
        load_observation_records(observations_path),
        limit=limit,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(review_pack, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return output_path


def promote_replay_candidate(
    candidate: Mapping[str, Any],
    *,
    replay_path: Path,
) -> dict[str, Any]:
    """Append an approved human-reviewed candidate to the replay dataset."""

    replay_sample = replay_sample_from_candidate(candidate)
    existing_samples = _load_replay_payload(replay_path)
    existing_ids = {str(sample.get("sample_id") or "") for sample in existing_samples}
    if replay_sample["sample_id"] in existing_ids:
        raise ValueError(f"Duplicate replay sample_id: {replay_sample['sample_id']}")
    existing_samples.append(replay_sample)
    replay_path.write_text(
        json.dumps(existing_samples, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return replay_sample


def replay_sample_from_candidate(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Convert an approved review candidate into replay JSON shape."""

    status = str(candidate.get("status") or "")
    if status != APPROVED_REPLAY_STATUS:
        raise ValueError("Replay candidate must be approved before promotion")

    message_text = str(candidate.get("message_text") or "").strip()
    if not message_text:
        raise ValueError("Approved replay candidate must include message_text")

    source_hash = str(candidate.get("source_observation_hash") or "")
    if source_hash and _hash_text(message_text) != source_hash:
        raise ValueError("Approved replay candidate message_text does not match hash")

    sample_id = str(
        candidate.get("sample_id") or candidate.get("sample_id_suggestion") or ""
    ).strip()
    if not sample_id:
        raise ValueError("Approved replay candidate must include sample_id")

    expected_slots = _required_mapping(candidate, "expected_slots")
    expected_rule_gate = _required_mapping(candidate, "expected_rule_gate")
    expected_final = _required_mapping(candidate, "expected_final")

    replay_sample: dict[str, Any] = {
        "sample_id": sample_id,
        "text": message_text,
        "metadata": _string_mapping(candidate.get("metadata") or {}),
        "expected_slots": _string_mapping(expected_slots),
        "expected_rule_gate": dict(expected_rule_gate),
        "expected_final": dict(expected_final),
    }
    attachment_kinds = candidate.get("attachment_kinds")
    if attachment_kinds:
        if not isinstance(attachment_kinds, list | tuple):
            raise TypeError("Approved replay candidate attachment_kinds must be a list")
        replay_sample["attachment_kinds"] = [str(kind) for kind in attachment_kinds]
    attachment_summary = candidate.get("attachment_summary")
    if attachment_summary:
        replay_sample["attachment_summary"] = str(attachment_summary)
    candidates = candidate.get("candidates")
    if candidates:
        if not isinstance(candidates, list | tuple):
            raise TypeError("Approved replay candidate candidates must be a list")
        replay_sample["candidates"] = [dict(candidate) for candidate in candidates]
    return replay_sample


def _proposal_reason(record: Mapping[str, Any]) -> str | None:
    slots = record.get("slots") if isinstance(record.get("slots"), Mapping) else {}
    gate = (
        record.get("rule_gate") if isinstance(record.get("rule_gate"), Mapping) else {}
    )
    final = record.get("final") if isinstance(record.get("final"), Mapping) else {}
    side_effects = (
        record.get("side_effects")
        if isinstance(record.get("side_effects"), Mapping)
        else {}
    )
    candidate_sources = str(record.get("candidate_sources") or "")
    if candidate_sources and gate.get("queue_allowed") == "false":
        return "llm_candidate_rule_gate_conflict"
    if (
        slots.get("rdf_next_action") == "preprocess_media"
        and slots.get("rdf_department_signal") == "department_context"
    ):
        return "media_before_department"
    if side_effects.get("disabled_legacy_cli"):
        return "disabled_legacy_cli"
    if side_effects.get("disabled_legacy_cli_card"):
        return "disabled_legacy_cli_card"
    if gate.get("queue_allowed") == "false" and str(
        final.get("provider_id") or ""
    ).startswith("cli/antigravity/"):
        return "disabled_legacy_cli_without_queue_reason"
    if final.get("intent") in {"casual", "fallback"}:
        return "employee_entry_baseline"
    if slots.get("rdf_department_signal") == "department_context":
        return "department_context_review"
    return None


def _count_by_key(items: Iterable[Mapping[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        value = str(item.get(key) or "")
        if not value:
            continue
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _count_nested(
    items: Iterable[Mapping[str, Any]],
    container_key: str,
    value_key: str,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        container = item.get(container_key)
        if not isinstance(container, Mapping):
            continue
        value = str(container.get(value_key) or "")
        if not value:
            continue
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _legacy_surface_priorities() -> list[dict[str, object]]:
    return [
        {
            "surface_id": surface.surface_id,
            "risk": surface.risk.value,
            "migration_action": surface.migration_action,
            "count": surface.count,
        }
        for surface in legacy_surfaces()
        if surface.risk is LegacySurfaceRisk.HIGH
    ]


def _normalized_slots(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    slots: dict[str, str] = {}
    for key, slot_value in value.items():
        normalized_key = str(key)
        if normalized_key.startswith(_RDF_SLOT_PREFIX):
            normalized_key = normalized_key.removeprefix(_RDF_SLOT_PREFIX)
        slots[normalized_key] = str(slot_value)
    return slots


def _required_mapping(candidate: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = candidate.get(key)
    if not isinstance(value, Mapping) or not value:
        raise ValueError(f"Approved replay candidate must include {key}")
    return value


def _string_mapping(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise TypeError("Replay candidate metadata must be an object")
    return {str(key): str(item) for key, item in value.items()}


def _load_replay_payload(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise TypeError("Router replay dataset must be a JSON array")
    samples: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            raise TypeError("Router replay dataset items must be objects")
        samples.append(dict(item))
    return samples


def _sample_id_suggestion(reason: str, text_hash: str) -> str:
    reason_slug = re.sub(r"[^a-z0-9_]+", "_", reason.lower()).strip("_")
    hash_suffix = text_hash[:10] if text_hash else "unhashed"
    return f"{reason_slug}_{hash_suffix}"


def _metadata(decision: Any) -> Mapping[str, Any]:
    metadata = getattr(decision, "metadata", None)
    return metadata if isinstance(metadata, Mapping) else {}


def _safe_call(event: Any, method_name: str) -> str:
    method = getattr(event, method_name, None)
    if not callable(method):
        return ""
    try:
        return str(method() or "")
    except Exception:  # noqa: BLE001
        return ""


def _event_extra(event: Any, key: str) -> Any:
    getter = getattr(event, "get_extra", None)
    if not callable(getter):
        return None
    try:
        return getter(key)
    except Exception:  # noqa: BLE001
        return None


def _hash_text(text: str) -> str:
    normalized = " ".join((text or "").split())
    return sha256(normalized.encode("utf-8")).hexdigest()


def _hash_identity(value: str) -> str:
    if not value:
        return ""
    return sha256(value.encode("utf-8")).hexdigest()[:16]


def _text_shape(text: str) -> dict[str, Any]:
    stripped = text or ""
    return {
        "length": len(stripped),
        "has_question": "?" in stripped or "？" in stripped,
        "has_hash_prefix": stripped.lstrip().startswith("#"),
        "has_attachment_marker": "[image]" in stripped.lower()
        or "[file]" in stripped.lower(),
    }


__all__ = [
    "APPROVED_REPLAY_STATUS",
    "DEFAULT_OBSERVATION_PATH",
    "append_observation_record",
    "build_observation_record",
    "build_replay_review_candidate",
    "build_replay_proposals",
    "build_review_pack",
    "capture_router_decision_observation",
    "load_observation_records",
    "promote_replay_candidate",
    "replay_sample_from_candidate",
    "write_replay_proposals",
    "write_review_pack",
]
