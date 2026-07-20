#!/usr/bin/env python3
"""Deterministic repair policy and execution engine for watchdog incidents."""

from __future__ import annotations

import argparse
import dataclasses
import fcntl
import hashlib
import hmac
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
MIN_AUTO_REPAIR_CONFIDENCE = 0.75
MAX_ATTEMPTS = 2
ATTEMPT_WINDOW_SECONDS = 3600
FAILURES_TO_OPEN_CIRCUIT = 2
CIRCUIT_OPEN_SECONDS = 21600
REVIEW_PLAN_TTL_SECONDS = 120
REVIEW_TRANSITIONS = {
    "acknowledge": ("pending", "acknowledged"),
    "resolve": ("acknowledged", "resolved"),
}
REVIEW_NOTIFICATION_STAGES = frozenset(
    {"created", "pending_15m", "pending_60m", "acknowledged_4h"}
)
ACTION_IDS = (
    "observe_only",
    "restart_managed_service",
    "manual_intervention",
)
AUTO_RESTART_TARGETS = {
    "astrbot_dashboard": "astrbot",
    "astrbot_response": "astrbot",
    "astrbot_api": "astrbot",
    "hermes_gateway": "hermes-gateway",
    "hermes_webui": "hermes-webui",
    "hermes_webui_thirdparty": "hermes-webui-thirdparty",
}


@dataclasses.dataclass(frozen=True, slots=True)
class RepairActionPolicy:
    """One deterministic action from the repair catalog.

    Attributes:
        action_id: Stable action identifier recorded in repair events.
        risk: Risk recomputed by policy rather than trusted from the model.
        command: Fixed command selected by the deterministic controller.
        rollback_command: Fixed rollback command for persistent mutations.
        timeout_seconds: Maximum execution time for either command.
        persistent_mutation: Whether a failed action requires rollback.
    """

    action_id: str
    risk: str
    command: tuple[str, ...]
    rollback_command: tuple[str, ...] | None = None
    timeout_seconds: int = 180
    persistent_mutation: bool = False


@dataclasses.dataclass(frozen=True, slots=True)
class RepairDecision:
    """Deterministic authorization decision for one proposal.

    Attributes:
        allowed: Whether the action may execute automatically.
        reason: Stable machine-readable decision reason.
        risk: Policy-computed risk level.
        action: Fixed action policy when execution is allowed.
    """

    allowed: bool
    reason: str
    risk: str
    action: RepairActionPolicy | None = None


def proposal_schema() -> dict[str, Any]:
    """Return the strict JSON schema used for Codex repair proposals.

    Returns:
        A JSON Schema that permits bounded action identifiers and no commands.
    """
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "incident_id",
            "service",
            "summary",
            "confidence",
            "action_id",
            "reason",
            "risk_hint",
        ],
        "properties": {
            "schema_version": {"type": "integer", "const": SCHEMA_VERSION},
            "incident_id": {"type": "string", "minLength": 1, "maxLength": 80},
            "service": {"type": "string", "minLength": 1, "maxLength": 120},
            "summary": {"type": "string", "minLength": 1, "maxLength": 800},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "action_id": {"type": "string", "enum": list(ACTION_IDS)},
            "reason": {"type": "string", "minLength": 1, "maxLength": 1000},
            "risk_hint": {
                "type": "string",
                "enum": ["none", "low", "medium", "high"],
            },
        },
    }


def diagnosis_bundle_schema() -> dict[str, Any]:
    """Return the schema for one combined diagnosis and repair proposal.

    Returns:
        A strict JSON Schema containing a Markdown report and bounded proposal.
    """
    nested_proposal = proposal_schema()
    nested_proposal.pop("$schema", None)
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": ["report_markdown", "proposal"],
        "properties": {
            "report_markdown": {
                "type": "string",
                "minLength": 1,
                "maxLength": 12000,
            },
            "proposal": nested_proposal,
        },
    }


def _validate_proposal(
    incident: dict[str, Any], proposal: dict[str, Any]
) -> str | None:
    required = set(proposal_schema()["required"])
    if set(proposal) != required:
        return "proposal_fields_invalid"
    if proposal.get("schema_version") != SCHEMA_VERSION:
        return "proposal_schema_version_invalid"
    if not isinstance(proposal.get("incident_id"), str):
        return "proposal_incident_id_invalid"
    if not isinstance(proposal.get("service"), str):
        return "proposal_service_invalid"
    if not isinstance(proposal.get("summary"), str) or not proposal["summary"]:
        return "proposal_summary_invalid"
    if not isinstance(proposal.get("reason"), str) or not proposal["reason"]:
        return "proposal_reason_invalid"
    confidence = proposal.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, int | float):
        return "proposal_confidence_invalid"
    if not 0 <= float(confidence) <= 1:
        return "proposal_confidence_invalid"
    if proposal.get("action_id") not in ACTION_IDS:
        return "proposal_action_invalid"
    if proposal.get("risk_hint") not in {"none", "low", "medium", "high"}:
        return "proposal_risk_hint_invalid"
    if proposal["incident_id"] != str(incident.get("incident_id", "")):
        return "proposal_incident_mismatch"
    if proposal["service"] != str(incident.get("service", "")):
        return "proposal_service_mismatch"
    if not str(incident.get("cur_status", "")).startswith("fail"):
        return "incident_not_failed"
    return None


def decide_repair(
    incident: dict[str, Any],
    proposal: dict[str, Any],
    *,
    dc_root: Path | str,
) -> RepairDecision:
    """Authorize a proposal and resolve it to a fixed local action.

    Args:
        incident: Watchdog incident snapshot.
        proposal: Structured Codex proposal.
        dc_root: DC-Agent repository root used to resolve trusted scripts.

    Returns:
        A fail-closed decision with policy-computed risk and command.
    """
    validation_error = _validate_proposal(incident, proposal)
    if validation_error:
        return RepairDecision(False, validation_error, "high")

    action_id = proposal["action_id"]
    if action_id == "observe_only":
        return RepairDecision(False, "observe_only", "none")
    if action_id == "manual_intervention":
        return RepairDecision(False, "manual_intervention_required", "high")
    if float(proposal["confidence"]) < MIN_AUTO_REPAIR_CONFIDENCE:
        return RepairDecision(False, "confidence_below_threshold", "low")

    service = str(incident["service"])
    restart_target = AUTO_RESTART_TARGETS.get(service)
    if not restart_target:
        return RepairDecision(False, "service_not_auto_repairable", "high")

    action = RepairActionPolicy(
        action_id="restart_managed_service",
        risk="low",
        command=(
            str(Path(dc_root) / "scripts-tools" / "safe_restart.sh"),
            restart_target,
        ),
        persistent_mutation=False,
    )
    return RepairDecision(True, "authorized_safe_restart", "low", action)


def verify_incident_probe(incident: dict[str, Any]) -> tuple[bool, str]:
    """Verify the original watchdog probe after a repair action.

    Args:
        incident: Snapshot containing the original ``kind:target`` probe.

    Returns:
        A pair containing the health result and a bounded detail string.
    """
    raw_probe = str(incident.get("probe", ""))
    if ":" not in raw_probe:
        return False, "probe_invalid"
    kind, target = raw_probe.split(":", 1)
    if kind == "tcp":
        try:
            with socket.create_connection(("127.0.0.1", int(target)), timeout=3):
                return True, "tcp_listening"
        except (OSError, ValueError):
            return False, "tcp_unavailable"
    if kind not in {"http", "http_strict"}:
        return False, "probe_kind_not_verifiable"

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(target, method="GET")
    try:
        with opener.open(request, timeout=3) as response:
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    except (OSError, ValueError, urllib.error.URLError):
        return False, "http_unavailable"
    if kind == "http_strict":
        healthy = 200 <= status < 300
    else:
        healthy = 200 <= status < 400 or status == 401
    return healthy, f"http_{status}"


def execute_action(
    action: RepairActionPolicy,
    incident: dict[str, Any],
    *,
    executor: Callable[..., subprocess.CompletedProcess[Any]],
    verifier: Callable[[dict[str, Any]], tuple[bool, str]],
    env: dict[str, str],
) -> dict[str, Any]:
    """Execute, verify, and optionally roll back one fixed action.

    Args:
        action: Deterministic action policy selected by the controller.
        incident: Original incident used for post-action verification.
        executor: Injectable subprocess runner.
        verifier: Injectable deterministic probe verifier.
        env: Sanitized execution environment.

    Returns:
        Bounded execution metadata without stdout, stderr, or arbitrary commands.
    """
    exit_code: int | None = None
    try:
        completed = executor(
            action.command,
            check=False,
            capture_output=True,
            text=True,
            timeout=action.timeout_seconds,
            env=env,
        )
        exit_code = completed.returncode
        if exit_code == 0:
            verification_ok, verification_detail = verifier(incident)
        else:
            verification_ok = False
            verification_detail = f"action_exit_{exit_code}"
    except subprocess.TimeoutExpired:
        verification_ok = False
        verification_detail = "action_timeout"
    except OSError:
        verification_ok = False
        verification_detail = "action_start_failed"

    if verification_ok:
        rollback = (
            "not_triggered"
            if action.persistent_mutation
            else "not_required_non_persistent_action"
        )
    elif action.rollback_command:
        try:
            rolled_back = executor(
                action.rollback_command,
                check=False,
                capture_output=True,
                text=True,
                timeout=action.timeout_seconds,
                env=env,
            )
            rollback = "succeeded" if rolled_back.returncode == 0 else "failed"
        except subprocess.TimeoutExpired:
            rollback = "timeout"
        except OSError:
            rollback = "start_failed"
    elif action.persistent_mutation:
        rollback = "missing_required_rollback"
    else:
        rollback = "not_required_non_persistent_action"

    return {
        "ok": verification_ok,
        "exit_code": exit_code,
        "verification": {
            "ok": verification_ok,
            "detail": verification_detail,
        },
        "rollback": rollback,
    }


def _load_json_object(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("JSON root must be an object")
    return data


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def split_diagnosis_bundle(
    *,
    bundle_path: Path,
    report_path: Path,
    proposal_path: Path,
) -> None:
    """Split a structured Codex result into human and controller inputs.

    Args:
        bundle_path: Combined Codex JSON output path.
        report_path: Destination for the human-readable Markdown report.
        proposal_path: Destination for the bounded repair proposal.

    Raises:
        ValueError: If the bundle is not the expected strict object.
    """
    bundle = _load_json_object(bundle_path)
    if set(bundle) != {"report_markdown", "proposal"}:
        raise ValueError("diagnosis bundle fields are invalid")
    report = bundle.get("report_markdown")
    proposal = bundle.get("proposal")
    if not isinstance(report, str) or not report.strip():
        raise ValueError("diagnosis report is invalid")
    if not isinstance(proposal, dict):
        raise ValueError("repair proposal is invalid")

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(f"{report.rstrip()}\n", encoding="utf-8")
    _write_json_atomic(proposal_path, proposal)


def collect_repair_status(
    *,
    state_path: Path,
    events_path: Path,
    analytics_path: Path | None = None,
    now: int | None = None,
    recent_limit: int = 20,
) -> dict[str, Any]:
    """Project repair budgets and outcomes into a bounded read-only snapshot.

    Args:
        state_path: Persistent retry and circuit-breaker state path.
        events_path: Append-only repair event log path.
        analytics_path: Optional derived read-only reliability snapshot path.
        now: Optional Unix timestamp for deterministic tests.
        recent_limit: Maximum number of recent sanitized results to return.

    Returns:
        JSON-safe policy, service, recent-result, and source-error state.
    """
    timestamp = int(time.time()) if now is None else int(now)
    source_errors: dict[str, str] = {}
    services: list[dict[str, Any]] = []
    reviews: list[dict[str, Any]] = []
    state_status = "empty"

    if state_path.exists():
        try:
            state = _load_json_object(state_path)
            if state.get("schema_version") != SCHEMA_VERSION:
                raise ValueError("unsupported repair state schema")
            raw_services = state.get("services", {})
            if not isinstance(raw_services, dict):
                raise ValueError("repair services state must be an object")
            state_status = "ready"
            for service, raw in sorted(raw_services.items()):
                if not isinstance(raw, dict):
                    source_errors["repair_state"] = (
                        "one or more service states are invalid"
                    )
                    continue
                all_attempts = [
                    int(value)
                    for value in raw.get("attempts", [])
                    if not isinstance(value, bool) and isinstance(value, int | float)
                ]
                attempts = [
                    value
                    for value in all_attempts
                    if 0 <= timestamp - value < ATTEMPT_WINDOW_SECONDS
                ]
                circuit_open_until = int(raw.get("circuit_open_until", 0) or 0)
                circuit_remaining = max(0, circuit_open_until - timestamp)
                last_outcome = str(raw.get("last_outcome") or "idle")[:80]
                current_state = (
                    "circuit_open"
                    if circuit_remaining
                    else last_outcome
                    if last_outcome in {"repaired", "failed"}
                    else "idle"
                )
                restart_target = AUTO_RESTART_TARGETS.get(str(service), "")
                groups = ["watchdog", "repair"]
                if restart_target:
                    groups.append(restart_target.split("-", 1)[0])
                last_attempt = max(all_attempts, default=0)
                services.append(
                    {
                        "service": str(service)[:120],
                        "state": current_state,
                        "groups": groups,
                        "last_outcome": last_outcome,
                        "attempt_count_window": len(attempts),
                        "attempts_remaining": max(0, MAX_ATTEMPTS - len(attempts)),
                        "consecutive_failures": int(
                            raw.get("consecutive_failures", 0) or 0
                        ),
                        "circuit_open_until": circuit_open_until,
                        "circuit_remaining_seconds": circuit_remaining,
                        "last_incident_id": str(raw.get("last_incident_id") or "")[
                            :120
                        ],
                        "last_attempt_at": (
                            datetime.fromtimestamp(last_attempt, UTC).isoformat()
                            if last_attempt
                            else ""
                        ),
                    }
                )
            raw_reviews = state.get("reviews", {})
            if not isinstance(raw_reviews, dict):
                raise ValueError("repair reviews state must be an object")
            for incident_id, raw in sorted(raw_reviews.items()):
                if not isinstance(raw, dict):
                    source_errors["repair_state"] = (
                        "one or more repair review states are invalid"
                    )
                    continue
                service = str(raw.get("service") or "")[:120]
                restart_target = AUTO_RESTART_TARGETS.get(service, "")
                groups = ["watchdog", "repair"]
                if restart_target:
                    groups.append(restart_target.split("-", 1)[0])
                raw_notifications = raw.get("notifications", {})
                notifications = (
                    {
                        str(stage): int(notified_at)
                        for stage, notified_at in raw_notifications.items()
                        if stage in REVIEW_NOTIFICATION_STAGES
                        and not isinstance(notified_at, bool)
                        and isinstance(notified_at, int | float)
                    }
                    if isinstance(raw_notifications, dict)
                    else {}
                )
                reviews.append(
                    {
                        "incident_id": str(incident_id)[:120],
                        "service": service,
                        "groups": groups,
                        "status": str(raw.get("status") or "pending")[:40],
                        "summary": str(raw.get("summary") or "")[:800],
                        "reason": str(raw.get("reason") or "")[:1000],
                        "risk": str(raw.get("risk") or "high")[:40],
                        "action_id": str(raw.get("action_id") or "manual_intervention")[
                            :80
                        ],
                        "created_at_unix": int(raw.get("created_at_unix", 0) or 0),
                        "updated_at_unix": int(raw.get("updated_at_unix", 0) or 0),
                        "notification_stages": [
                            stage
                            for stage, _notified_at in sorted(
                                notifications.items(), key=lambda item: item[1]
                            )
                        ],
                        "last_notification_at_unix": max(
                            notifications.values(), default=0
                        ),
                    }
                )
        except (OSError, ValueError, json.JSONDecodeError, TypeError) as exc:
            state_status = "invalid"
            source_errors["repair_state"] = str(exc)[:300]

    recent_results: deque[dict[str, Any]] = deque(
        maxlen=max(1, min(int(recent_limit), 100))
    )
    invalid_event_rows = 0
    if events_path.exists():
        try:
            with events_path.open(encoding="utf-8") as stream:
                for line in stream:
                    try:
                        raw = json.loads(line)
                    except json.JSONDecodeError:
                        invalid_event_rows += 1
                        continue
                    if not isinstance(raw, dict):
                        invalid_event_rows += 1
                        continue
                    service = str(raw.get("service") or "")[:120]
                    restart_target = AUTO_RESTART_TARGETS.get(service, "")
                    groups = ["watchdog", "repair"]
                    if restart_target:
                        groups.append(restart_target.split("-", 1)[0])
                    verification = raw.get("verification", {})
                    preflight = raw.get("preflight_verification", {})
                    recent_results.append(
                        {
                            "ts_unix": int(raw.get("ts_unix", 0) or 0),
                            "incident_id": str(raw.get("incident_id") or "")[:120],
                            "service": service,
                            "groups": groups,
                            "action_id": str(raw.get("action_id") or "")[:80],
                            "status": str(raw.get("status") or "unknown")[:80],
                            "reason": str(raw.get("reason") or "")[:160],
                            "risk": str(raw.get("risk") or "unknown")[:40],
                            "attempt_count": int(raw.get("attempt_count", 0) or 0),
                            "verification_detail": (
                                str(verification.get("detail") or "")[:120]
                                if isinstance(verification, dict)
                                else ""
                            ),
                            "preflight_detail": (
                                str(preflight.get("detail") or "")[:120]
                                if isinstance(preflight, dict)
                                else ""
                            ),
                            "rollback": str(raw.get("rollback") or "")[:80],
                            "circuit_open_until": int(
                                raw.get("circuit_open_until", 0) or 0
                            ),
                        }
                    )
        except (OSError, TypeError, ValueError) as exc:
            source_errors["repair_events"] = str(exc)[:300]
    if invalid_event_rows:
        source_errors["repair_events"] = (
            f"{invalid_event_rows} invalid repair event row(s)"
        )

    analytics: dict[str, Any] = {}
    if analytics_path is not None and analytics_path.exists():
        try:
            raw_analytics = _load_json_object(analytics_path)
            dashboard_summary = raw_analytics.get("dashboard_summary")
            if (
                raw_analytics.get("schema_version") != SCHEMA_VERSION
                or raw_analytics.get("mode") != "read_only"
                or not isinstance(dashboard_summary, dict)
            ):
                raise ValueError("repair analytics snapshot is invalid")
            status = dashboard_summary.get("status")
            if status not in {"healthy", "degraded", "critical"}:
                raise ValueError("repair analytics status is invalid")
            count_fields = (
                "auto_attempts_24h",
                "open_circuits",
                "open_reviews",
                "pending_over_60m",
                "retention_candidates",
            )
            if any(
                isinstance(dashboard_summary.get(field), bool)
                or not isinstance(dashboard_summary.get(field), int | float)
                for field in count_fields
            ) or not isinstance(
                dashboard_summary.get("codex_review_recommended"), bool
            ):
                raise ValueError("repair analytics summary is invalid")
            optional_number_fields = (
                "success_rate_24h",
                "mttr_mean_seconds_24h",
            )
            if any(
                dashboard_summary.get(field) is not None
                and (
                    isinstance(dashboard_summary.get(field), bool)
                    or not isinstance(dashboard_summary.get(field), int | float)
                )
                for field in optional_number_fields
            ):
                raise ValueError("repair analytics metrics are invalid")
            analytics = {
                "schema_version": SCHEMA_VERSION,
                "generated_at_unix": int(
                    raw_analytics.get("generated_at_unix", 0) or 0
                ),
                "mode": "read_only",
                "dashboard_summary": {
                    "status": status,
                    **{field: int(dashboard_summary[field]) for field in count_fields},
                    "success_rate_24h": dashboard_summary.get("success_rate_24h"),
                    "mttr_mean_seconds_24h": dashboard_summary.get(
                        "mttr_mean_seconds_24h"
                    ),
                    "codex_review_recommended": dashboard_summary[
                        "codex_review_recommended"
                    ],
                },
            }
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            analytics = {}
            source_errors["repair_analytics"] = str(exc)[:300]

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at_unix": timestamp,
        "mode": "read_only",
        "state_status": state_status,
        "policy": {
            "max_attempts": MAX_ATTEMPTS,
            "attempt_window_seconds": ATTEMPT_WINDOW_SECONDS,
            "failures_to_open_circuit": FAILURES_TO_OPEN_CIRCUIT,
            "circuit_open_seconds": CIRCUIT_OPEN_SECONDS,
        },
        "services": services,
        "service_count": len(services),
        "recent_results": list(recent_results),
        "recent_result_count": len(recent_results),
        "reviews": reviews,
        "review_count": len(reviews),
        "pending_review_count": sum(
            review["status"] != "resolved" for review in reviews
        ),
        "analytics": analytics,
        "source_errors": source_errors,
    }


def build_review_control_plan(
    *,
    state_path: Path,
    incident_id: str,
    operation: str,
    issued_at: int | None = None,
) -> dict[str, Any]:
    """Build an exact short-lived plan for one review state transition.

    Args:
        state_path: Persistent repair state containing the review queue.
        incident_id: Exact queued incident to review.
        operation: Review transition, ``acknowledge`` or ``resolve``.
        issued_at: Optional Unix timestamp for deterministic tests.

    Returns:
        A state-bound Control Plan that contains no runtime repair action.

    Raises:
        ValueError: If the operation, state, review, or transition is invalid.
    """
    if operation not in REVIEW_TRANSITIONS:
        raise ValueError(f"unsupported review operation: {operation}")
    try:
        state = _load_json_object(state_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("repair review state is unavailable") from exc
    if state.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("repair review state schema is invalid")
    reviews = state.get("reviews")
    if not isinstance(reviews, dict):
        raise ValueError("repair review queue is unavailable")
    review = reviews.get(incident_id)
    if not isinstance(review, dict):
        raise ValueError(f"repair review not found: {incident_id}")
    from_status, to_status = REVIEW_TRANSITIONS[operation]
    if review.get("status") != from_status:
        raise ValueError(f"review {operation} requires status {from_status}")

    timestamp = int(time.time()) if issued_at is None else int(issued_at)
    review_fingerprint = hashlib.sha256(
        json.dumps(review, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    canonical = {
        "schema_version": SCHEMA_VERSION,
        "scope": "repair_review",
        "operation": operation,
        "incident_id": incident_id,
        "service": str(review.get("service") or ""),
        "from_status": from_status,
        "to_status": to_status,
        "review_fingerprint": review_fingerprint,
        "issued_at": timestamp,
    }
    digest = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:32]
    return {
        **canonical,
        "plan_id": f"{timestamp}.{digest}",
        "issued_at_unix": timestamp,
        "expires_at_unix": timestamp + REVIEW_PLAN_TTL_SECONDS,
        "requires_confirmation": True,
        "impact_summary": (
            "Updates review metadata only; does not execute repair commands, "
            "reset circuits, or consume retry budget."
        ),
    }


def apply_review_control_plan(
    *,
    state_path: Path,
    incident_id: str,
    operation: str,
    confirm_plan: str | None,
    now: int | None = None,
) -> dict[str, Any]:
    """Apply one confirmed review-only Control Plan atomically.

    Args:
        state_path: Persistent repair state containing the review queue.
        incident_id: Exact queued incident bound to the plan.
        operation: Review transition, ``acknowledge`` or ``resolve``.
        confirm_plan: Exact short-lived plan ID returned by the planner.
        now: Optional Unix timestamp for deterministic tests.

    Returns:
        Applied plan metadata and the resulting review status.

    Raises:
        ValueError: If confirmation is missing, expired, stale, or busy.
    """
    if not confirm_plan:
        raise ValueError("review Control Plan confirmation is required")
    try:
        issued_at = int(confirm_plan.split(".", 1)[0])
    except (AttributeError, ValueError) as exc:
        raise ValueError("review Control Plan confirmation is invalid") from exc
    timestamp = int(time.time()) if now is None else int(now)
    if issued_at > timestamp + 5 or timestamp - issued_at > REVIEW_PLAN_TTL_SECONDS:
        raise ValueError("review Control Plan confirmation is expired")
    if not state_path.exists():
        raise ValueError("repair review state is unavailable")

    lock_path = state_path.with_name(f"{state_path.name}.lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("repair review state is busy") from exc
        try:
            plan = build_review_control_plan(
                state_path=state_path,
                incident_id=incident_id,
                operation=operation,
                issued_at=issued_at,
            )
        except ValueError as exc:
            raise ValueError("review Control Plan confirmation is stale") from exc
        if not hmac.compare_digest(plan["plan_id"], confirm_plan):
            raise ValueError("review Control Plan confirmation is stale")

        state = _load_json_object(state_path)
        review = state["reviews"][incident_id]
        review["status"] = plan["to_status"]
        review["updated_at_unix"] = timestamp
        review[f"{plan['to_status']}_at_unix"] = timestamp
        review["reviewed_by"] = "local_operator"
        _write_json_atomic(state_path, state)
        return {
            **plan,
            "applied_at_unix": timestamp,
            "status": "applied",
        }


def collect_due_review_notifications(
    *,
    state_path: Path,
    now: int | None = None,
) -> list[dict[str, Any]]:
    """Collect the highest due notification stage for each open review.

    Args:
        state_path: Persistent repair state containing the review queue.
        now: Optional Unix timestamp for deterministic tests.

    Returns:
        Bounded notification candidates without commands or arbitrary actions.

    Raises:
        ValueError: If the repair review state is invalid.
    """
    timestamp = int(time.time()) if now is None else int(now)
    try:
        state = _load_json_object(state_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("repair review state is unavailable") from exc
    if state.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("repair review state schema is invalid")
    reviews = state.get("reviews", {})
    if not isinstance(reviews, dict):
        raise ValueError("repair review queue is invalid")

    due: list[dict[str, Any]] = []
    for incident_id, review in sorted(reviews.items()):
        if not isinstance(review, dict):
            continue
        status = str(review.get("status") or "pending")
        if status == "resolved":
            continue
        notifications = review.get("notifications", {})
        if not isinstance(notifications, dict):
            notifications = {}
        stage = ""
        level = "warning"
        age_seconds = 0
        if status == "pending":
            created_at = int(review.get("created_at_unix", 0) or 0)
            age_seconds = max(0, timestamp - created_at)
            if age_seconds >= 3600:
                if "pending_60m" not in notifications:
                    stage = "pending_60m"
                    level = "critical"
            elif age_seconds >= 900:
                if "pending_15m" not in notifications:
                    stage = "pending_15m"
            elif "created" not in notifications:
                stage = "created"
                level = "critical"
        elif status == "acknowledged":
            acknowledged_at = int(
                review.get("acknowledged_at_unix") or review.get("updated_at_unix") or 0
            )
            age_seconds = max(0, timestamp - acknowledged_at)
            if age_seconds >= 14400 and "acknowledged_4h" not in notifications:
                stage = "acknowledged_4h"
                level = "warning"
        if not stage:
            continue
        due.append(
            {
                "schema_version": SCHEMA_VERSION,
                "incident_id": str(incident_id)[:120],
                "service": str(review.get("service") or "")[:120],
                "review_status": status,
                "stage": stage,
                "level": level,
                "age_seconds": age_seconds,
                "summary": str(review.get("summary") or "")[:800],
                "reason": str(review.get("reason") or "")[:1000],
                "risk": str(review.get("risk") or "high")[:40],
            }
        )
    return due


def mark_review_notification(
    *,
    state_path: Path,
    incident_id: str,
    stage: str,
    notified_at: int | None = None,
) -> bool:
    """Record one successfully delivered review notification stage.

    Args:
        state_path: Persistent repair state containing the review queue.
        incident_id: Exact review incident that was notified.
        stage: Fixed notification stage.
        notified_at: Optional Unix timestamp for deterministic tests.

    Returns:
        Whether the state changed. Existing stages remain idempotent.

    Raises:
        ValueError: If the stage, state, or review is invalid or busy.
    """
    if stage not in REVIEW_NOTIFICATION_STAGES:
        raise ValueError(f"unsupported review notification stage: {stage}")
    if not state_path.exists():
        raise ValueError("repair review state is unavailable")
    timestamp = int(time.time()) if notified_at is None else int(notified_at)
    lock_path = state_path.with_name(f"{state_path.name}.lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("repair review state is busy") from exc
        state = _load_json_object(state_path)
        reviews = state.get("reviews")
        if state.get("schema_version") != SCHEMA_VERSION or not isinstance(
            reviews, dict
        ):
            raise ValueError("repair review state is invalid")
        review = reviews.get(incident_id)
        if not isinstance(review, dict):
            raise ValueError(f"repair review not found: {incident_id}")
        notifications = review.setdefault("notifications", {})
        if not isinstance(notifications, dict):
            raise ValueError("repair review notifications state is invalid")
        if stage in notifications:
            return False
        notifications[stage] = timestamp
        _write_json_atomic(state_path, state)
        return True


def _record_result(
    result_path: Path,
    events_path: Path,
    result: dict[str, Any],
) -> dict[str, Any]:
    _write_json_atomic(result_path, result)
    events_path.parent.mkdir(parents=True, exist_ok=True)
    with events_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(result, ensure_ascii=False) + "\n")
    return result


def run_repair(
    *,
    incident_path: Path,
    proposal_path: Path,
    state_path: Path,
    result_path: Path,
    events_path: Path,
    dc_root: Path | str,
    executor: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    verifier: Callable[[dict[str, Any]], tuple[bool, str]] = verify_incident_probe,
    now: int | None = None,
) -> dict[str, Any]:
    """Run one idempotent repair attempt under deterministic safety policy.

    Args:
        incident_path: Watchdog incident snapshot path.
        proposal_path: Strict Codex repair proposal path.
        state_path: Persistent retry and circuit-breaker state path.
        result_path: Per-incident repair result path.
        events_path: Append-only repair event log path.
        dc_root: DC-Agent repository root.
        executor: Injectable subprocess runner used for fixed commands.
        verifier: Injectable post-action health verifier.
        now: Optional Unix timestamp for deterministic tests.

    Returns:
        Machine-readable repair result with no command output.
    """
    timestamp = int(time.time()) if now is None else int(now)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_path.with_name(f"{state_path.name}.lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return _record_result(
                result_path,
                events_path,
                {
                    "schema_version": SCHEMA_VERSION,
                    "ts_unix": timestamp,
                    "incident_id": "unknown",
                    "service": "unknown",
                    "action_id": "unknown",
                    "status": "denied",
                    "reason": "repair_in_progress",
                    "risk": "high",
                },
            )

        try:
            incident = _load_json_object(incident_path)
            proposal = _load_json_object(proposal_path)
        except (OSError, ValueError, json.JSONDecodeError):
            return _record_result(
                result_path,
                events_path,
                {
                    "schema_version": SCHEMA_VERSION,
                    "ts_unix": timestamp,
                    "incident_id": "unknown",
                    "service": "unknown",
                    "action_id": "unknown",
                    "status": "denied",
                    "reason": "invalid_incident_or_proposal",
                    "risk": "high",
                },
            )

        incident_id = str(incident.get("incident_id", ""))
        service = str(incident.get("service", ""))
        action_id = str(proposal.get("action_id", "unknown"))
        incident_started_at_unix = 0
        raw_incident_timestamp = incident.get("ts_unix") or incident.get("ts")
        if not isinstance(raw_incident_timestamp, bool) and isinstance(
            raw_incident_timestamp, int | float
        ):
            incident_started_at_unix = int(raw_incident_timestamp)
        elif isinstance(raw_incident_timestamp, str) and raw_incident_timestamp:
            try:
                incident_started_at_unix = int(
                    datetime.fromisoformat(
                        raw_incident_timestamp.replace("Z", "+00:00")
                    ).timestamp()
                )
            except ValueError:
                incident_started_at_unix = 0
        common = {
            "schema_version": SCHEMA_VERSION,
            "ts_unix": timestamp,
            "incident_id": incident_id,
            "service": service,
            "action_id": action_id,
            **(
                {
                    "incident_started_at_unix": incident_started_at_unix,
                    "duration_seconds": max(0, timestamp - incident_started_at_unix),
                }
                if incident_started_at_unix
                else {}
            ),
        }
        try:
            state = _load_json_object(state_path) if state_path.exists() else {}
        except (OSError, ValueError, json.JSONDecodeError):
            return _record_result(
                result_path,
                events_path,
                {
                    **common,
                    "status": "denied",
                    "reason": "repair_state_invalid",
                    "risk": "high",
                },
            )
        state.setdefault("schema_version", SCHEMA_VERSION)
        incidents = state.setdefault("incidents", {})
        services = state.setdefault("services", {})
        reviews = state.setdefault("reviews", {})
        if (
            not isinstance(incidents, dict)
            or not isinstance(services, dict)
            or not isinstance(reviews, dict)
        ):
            return _record_result(
                result_path,
                events_path,
                {
                    **common,
                    "status": "denied",
                    "reason": "repair_state_invalid",
                    "risk": "high",
                },
            )
        if incident_id in incidents:
            service_state = services.get(service, {})
            return _record_result(
                result_path,
                events_path,
                {
                    **common,
                    "status": "denied",
                    "reason": "incident_already_handled",
                    "risk": "low",
                    "attempt_count": len(service_state.get("attempts", [])),
                    "circuit_open_until": service_state.get("circuit_open_until", 0),
                },
            )

        decision = decide_repair(incident, proposal, dc_root=dc_root)
        if not decision.allowed:
            if decision.reason == "manual_intervention_required":
                # Diagnosis can finish after a transient failure has recovered.
                # Recheck the exact probe before opening a durable review item.
                preflight_ok, preflight_detail = verifier(incident)
                if preflight_ok:
                    incidents[incident_id] = {
                        "status": "no_action",
                        "reason": "service_already_recovered",
                        "ts_unix": timestamp,
                    }
                    _write_json_atomic(state_path, state)
                    return _record_result(
                        result_path,
                        events_path,
                        {
                            **common,
                            "status": "no_action",
                            "reason": "service_already_recovered",
                            "risk": "none",
                            "preflight_verification": {
                                "ok": True,
                                "detail": preflight_detail,
                            },
                        },
                    )
            if decision.reason == "observe_only":
                incidents[incident_id] = {
                    "status": "no_action",
                    "reason": decision.reason,
                    "ts_unix": timestamp,
                }
                _write_json_atomic(state_path, state)
            elif decision.reason == "manual_intervention_required":
                incidents[incident_id] = {
                    "status": "review_required",
                    "reason": decision.reason,
                    "ts_unix": timestamp,
                }
                reviews[incident_id] = {
                    "service": service,
                    "status": "pending",
                    "created_at_unix": timestamp,
                    "updated_at_unix": timestamp,
                    "summary": str(proposal["summary"])[:800],
                    "reason": str(proposal["reason"])[:1000],
                    "risk": decision.risk,
                    "action_id": action_id,
                }
                _write_json_atomic(state_path, state)
            status = (
                "no_action"
                if decision.reason == "observe_only"
                else "review_required"
                if decision.reason == "manual_intervention_required"
                else "denied"
            )
            return _record_result(
                result_path,
                events_path,
                {
                    **common,
                    "status": status,
                    "reason": decision.reason,
                    "risk": decision.risk,
                    **(
                        {"review_status": "pending"}
                        if decision.reason == "manual_intervention_required"
                        else {}
                    ),
                },
            )

        preflight_ok, preflight_detail = verifier(incident)
        if preflight_ok:
            incidents[incident_id] = {
                "status": "no_action",
                "reason": "service_already_recovered",
                "ts_unix": timestamp,
            }
            _write_json_atomic(state_path, state)
            return _record_result(
                result_path,
                events_path,
                {
                    **common,
                    "status": "no_action",
                    "reason": "service_already_recovered",
                    "risk": "none",
                    "preflight_verification": {
                        "ok": True,
                        "detail": preflight_detail,
                    },
                },
            )

        service_state = services.setdefault(service, {})
        attempts = service_state.get("attempts", [])
        if not isinstance(attempts, list):
            attempts = []
        attempts = [
            int(item)
            for item in attempts
            if isinstance(item, int | float)
            and timestamp - int(item) < ATTEMPT_WINDOW_SECONDS
        ]
        circuit_open_until = int(service_state.get("circuit_open_until", 0) or 0)
        if circuit_open_until > timestamp:
            incidents[incident_id] = {
                "status": "denied",
                "reason": "circuit_open",
                "ts_unix": timestamp,
            }
            _write_json_atomic(state_path, state)
            return _record_result(
                result_path,
                events_path,
                {
                    **common,
                    "status": "denied",
                    "reason": "circuit_open",
                    "risk": decision.risk,
                    "attempt_count": len(attempts),
                    "circuit_open_until": circuit_open_until,
                },
            )
        if len(attempts) >= MAX_ATTEMPTS:
            service_state["circuit_open_until"] = timestamp + CIRCUIT_OPEN_SECONDS
            incidents[incident_id] = {
                "status": "denied",
                "reason": "retry_budget_exhausted",
                "ts_unix": timestamp,
            }
            _write_json_atomic(state_path, state)
            return _record_result(
                result_path,
                events_path,
                {
                    **common,
                    "status": "denied",
                    "reason": "retry_budget_exhausted",
                    "risk": decision.risk,
                    "attempt_count": len(attempts),
                    "circuit_open_until": service_state["circuit_open_until"],
                },
            )

        assert decision.action is not None
        if decision.action.persistent_mutation and not decision.action.rollback_command:
            return _record_result(
                result_path,
                events_path,
                {
                    **common,
                    "status": "denied",
                    "reason": "persistent_action_missing_rollback",
                    "risk": "high",
                },
            )

        attempts.append(timestamp)
        service_state["attempts"] = attempts
        incidents[incident_id] = {
            "status": "running",
            "ts_unix": timestamp,
        }
        _write_json_atomic(state_path, state)

        env = os.environ.copy()
        env["SAFE_RESTART_WATCHDOG_QUIET_SECONDS"] = "0"
        execution = execute_action(
            decision.action,
            incident,
            executor=executor,
            verifier=verifier,
            env=env,
        )
        consecutive_failures = int(service_state.get("consecutive_failures", 0) or 0)
        if execution["ok"]:
            status = "repaired"
            reason = "repair_verified"
            consecutive_failures = 0
            service_state["circuit_open_until"] = 0
        else:
            status = "failed"
            reason = "repair_verification_failed"
            consecutive_failures += 1
            if consecutive_failures >= FAILURES_TO_OPEN_CIRCUIT:
                service_state["circuit_open_until"] = timestamp + CIRCUIT_OPEN_SECONDS
        service_state["consecutive_failures"] = consecutive_failures
        service_state["last_outcome"] = status
        service_state["last_incident_id"] = incident_id
        incidents[incident_id] = {
            "status": status,
            "reason": reason,
            "ts_unix": timestamp,
        }
        _write_json_atomic(state_path, state)
        completed_at_unix = int(time.time()) if now is None else timestamp

        return _record_result(
            result_path,
            events_path,
            {
                **common,
                "status": status,
                "reason": reason,
                "risk": decision.risk,
                "attempt_count": len(attempts),
                "preflight_verification": {
                    "ok": False,
                    "detail": preflight_detail,
                },
                "execution_exit_code": execution["exit_code"],
                "verification": execution["verification"],
                "rollback": execution["rollback"],
                "circuit_open_until": service_state.get("circuit_open_until", 0),
                "completed_at_unix": completed_at_unix,
                **(
                    {
                        "duration_seconds": max(
                            0, completed_at_unix - incident_started_at_unix
                        )
                    }
                    if incident_started_at_unix
                    else {}
                ),
            },
        )


def main(argv: list[str] | None = None) -> int:
    """Run the repair schema or apply CLI.

    Args:
        argv: Optional command-line arguments. Defaults to ``sys.argv``.

    Returns:
        Zero for a recorded decision, otherwise two for CLI misuse.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("schema")
    subparsers.add_parser("bundle-schema")
    split_parser = subparsers.add_parser("split-bundle")
    split_parser.add_argument("--bundle", type=Path, required=True)
    split_parser.add_argument("--report", type=Path, required=True)
    split_parser.add_argument("--proposal", type=Path, required=True)
    mark_notification_parser = subparsers.add_parser("mark-review-notification")
    mark_notification_parser.add_argument("--state", type=Path, required=True)
    mark_notification_parser.add_argument("--incident", required=True)
    mark_notification_parser.add_argument(
        "--stage",
        choices=sorted(REVIEW_NOTIFICATION_STAGES),
        required=True,
    )
    apply_parser = subparsers.add_parser("apply")
    apply_parser.add_argument("--incident", type=Path, required=True)
    apply_parser.add_argument("--proposal", type=Path, required=True)
    apply_parser.add_argument("--state", type=Path, required=True)
    apply_parser.add_argument("--result", type=Path, required=True)
    apply_parser.add_argument("--events", type=Path, required=True)
    apply_parser.add_argument("--dc-root", type=Path, required=True)
    args = parser.parse_args(argv)

    if args.command == "schema":
        print(json.dumps(proposal_schema(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "bundle-schema":
        print(json.dumps(diagnosis_bundle_schema(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "split-bundle":
        split_diagnosis_bundle(
            bundle_path=args.bundle,
            report_path=args.report,
            proposal_path=args.proposal,
        )
        return 0
    if args.command == "mark-review-notification":
        mark_review_notification(
            state_path=args.state,
            incident_id=args.incident,
            stage=args.stage,
        )
        return 0
    if args.command == "apply":
        result = run_repair(
            incident_path=args.incident,
            proposal_path=args.proposal,
            state_path=args.state,
            result_path=args.result,
            events_path=args.events,
            dc_root=args.dc_root,
        )
        print(json.dumps(result, ensure_ascii=False))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
