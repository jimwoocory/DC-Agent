#!/usr/bin/env python3
"""Build a bounded read-only reliability snapshot for watchdog repairs."""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DC_ROOT = Path(__file__).resolve().parents[1]
WATCHDOG_ROOT = DC_ROOT / "data" / "watchdog"
STATE_PATH = WATCHDOG_ROOT / "repair_state.json"
REPAIR_EVENTS_PATH = WATCHDOG_ROOT / "repairs.jsonl"
NOTIFICATION_EVENTS_PATH = WATCHDOG_ROOT / "review_notifications.jsonl"
OUTPUT_PATH = WATCHDOG_ROOT / "repair_analytics.json"

SCHEMA_VERSION = 1
DEFAULT_REFRESH_INTERVAL_SECONDS = 300
METRIC_WINDOWS = {"24h": 86_400, "7d": 604_800}
REPAIR_EVENT_RETENTION_SECONDS = 7_776_000
NOTIFICATION_EVENT_RETENTION_SECONDS = 7_776_000
INCIDENT_STATE_RETENTION_SECONDS = 2_592_000
RESOLVED_REVIEW_RETENTION_SECONDS = 7_776_000


def _load_json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return payload


def _read_jsonl(path: Path) -> tuple[list[dict[str, Any]], int]:
    rows: list[dict[str, Any]] = []
    invalid_rows = 0
    if not path.exists():
        return rows, invalid_rows
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                invalid_rows += 1
                continue
            if not isinstance(row, dict):
                invalid_rows += 1
                continue
            rows.append(row)
    return rows, invalid_rows


def build_analytics(
    *,
    state_path: Path,
    repair_events_path: Path,
    notification_events_path: Path,
    now: int | None = None,
) -> dict[str, Any]:
    """Aggregate repair reliability without changing any source record.

    Args:
        state_path: Repair retry, circuit, incident, and review state.
        repair_events_path: Append-only repair result log.
        notification_events_path: Append-only review notification log.
        now: Optional Unix timestamp for deterministic tests.

    Returns:
        A bounded, JSON-safe reliability and retention-preview snapshot.
    """
    timestamp = int(time.time()) if now is None else int(now)
    source_errors: dict[str, str] = {}
    state: dict[str, Any] = {}
    if state_path.exists():
        try:
            state = _load_json_object(state_path)
            if state.get("schema_version") != SCHEMA_VERSION:
                raise ValueError("unsupported repair state schema")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            source_errors["repair_state"] = str(exc)[:300]
            state = {}

    try:
        repair_events, invalid_repair_rows = _read_jsonl(repair_events_path)
    except OSError as exc:
        repair_events = []
        invalid_repair_rows = 0
        source_errors["repair_events"] = str(exc)[:300]
    if invalid_repair_rows:
        source_errors["repair_events"] = (
            f"{invalid_repair_rows} invalid repair event row(s)"
        )

    try:
        notification_events, invalid_notification_rows = _read_jsonl(
            notification_events_path
        )
    except OSError as exc:
        notification_events = []
        invalid_notification_rows = 0
        source_errors["notification_events"] = str(exc)[:300]
    if invalid_notification_rows:
        source_errors["notification_events"] = (
            f"{invalid_notification_rows} invalid notification event row(s)"
        )

    windows: dict[str, dict[str, int | float | None]] = {}
    for label, window_seconds in METRIC_WINDOWS.items():
        lower_bound = timestamp - window_seconds
        result_rows = [
            row
            for row in repair_events
            if not isinstance(row.get("ts_unix"), bool)
            and isinstance(row.get("ts_unix"), int | float)
            and int(row["ts_unix"]) >= lower_bound
        ]
        attempts = [
            row
            for row in result_rows
            if row.get("action_id") == "restart_managed_service"
            and row.get("status") in {"repaired", "failed"}
        ]
        repaired = sum(row.get("status") == "repaired" for row in attempts)
        failed = sum(row.get("status") == "failed" for row in attempts)
        successful_durations = sorted(
            int(row["duration_seconds"])
            for row in attempts
            if row.get("status") == "repaired"
            and not isinstance(row.get("duration_seconds"), bool)
            and isinstance(row.get("duration_seconds"), int | float)
            and int(row["duration_seconds"]) >= 0
        )
        notification_count = sum(
            not isinstance(row.get("ts_unix"), bool)
            and isinstance(row.get("ts_unix"), int | float)
            and int(row["ts_unix"]) >= lower_bound
            and row.get("status") == "sent"
            for row in notification_events
        )
        windows[label] = {
            "result_count": len(result_rows),
            "auto_attempts": len(attempts),
            "repaired": repaired,
            "failed": failed,
            "success_rate": round(repaired / len(attempts), 4) if attempts else None,
            "mttr_sample_count": len(successful_durations),
            "mttr_mean_seconds": (
                round(sum(successful_durations) / len(successful_durations), 2)
                if successful_durations
                else None
            ),
            "mttr_p95_seconds": (
                successful_durations[
                    max(0, math.ceil(len(successful_durations) * 0.95) - 1)
                ]
                if successful_durations
                else None
            ),
            "notification_count": notification_count,
        }

    reviews_state = state.get("reviews", {})
    if not isinstance(reviews_state, dict):
        reviews_state = {}
        source_errors["repair_state"] = "repair reviews state must be an object"
    pending = 0
    acknowledged = 0
    pending_over_15m = 0
    pending_over_60m = 0
    acknowledged_over_4h = 0
    oldest_open_age = 0
    for review in reviews_state.values():
        if not isinstance(review, dict):
            continue
        status = str(review.get("status") or "pending")
        created_at = int(review.get("created_at_unix", 0) or 0)
        if status == "pending":
            pending += 1
            age = max(0, timestamp - created_at)
            pending_over_15m += age >= 900
            pending_over_60m += age >= 3_600
            oldest_open_age = max(oldest_open_age, age)
        elif status == "acknowledged":
            acknowledged += 1
            acknowledged_at = int(
                review.get("acknowledged_at_unix")
                or review.get("updated_at_unix")
                or created_at
            )
            age = max(0, timestamp - acknowledged_at)
            acknowledged_over_4h += age >= 14_400
            oldest_open_age = max(oldest_open_age, max(0, timestamp - created_at))
    review_metrics = {
        "open_count": pending + acknowledged,
        "pending_count": pending,
        "acknowledged_count": acknowledged,
        "pending_over_15m": pending_over_15m,
        "pending_over_60m": pending_over_60m,
        "acknowledged_over_4h": acknowledged_over_4h,
        "oldest_open_age_seconds": oldest_open_age,
    }

    services_state = state.get("services", {})
    if not isinstance(services_state, dict):
        services_state = {}
        source_errors["repair_state"] = "repair services state must be an object"
    open_circuit_services = sorted(
        str(service)[:120]
        for service, service_state in services_state.items()
        if isinstance(service_state, dict)
        and not isinstance(service_state.get("circuit_open_until"), bool)
        and isinstance(service_state.get("circuit_open_until"), int | float)
        and int(service_state["circuit_open_until"]) > timestamp
    )
    circuits = {
        "open_count": len(open_circuit_services),
        "services": open_circuit_services,
    }

    incidents_state = state.get("incidents", {})
    if not isinstance(incidents_state, dict):
        incidents_state = {}
        source_errors["repair_state"] = "repair incidents state must be an object"
    old_repair_events = sum(
        not isinstance(row.get("ts_unix"), bool)
        and isinstance(row.get("ts_unix"), int | float)
        and int(row["ts_unix"]) < timestamp - REPAIR_EVENT_RETENTION_SECONDS
        for row in repair_events
    )
    old_notification_events = sum(
        not isinstance(row.get("ts_unix"), bool)
        and isinstance(row.get("ts_unix"), int | float)
        and int(row["ts_unix"]) < timestamp - NOTIFICATION_EVENT_RETENTION_SECONDS
        for row in notification_events
    )
    old_incidents = sum(
        isinstance(incident, dict)
        and not isinstance(incident.get("ts_unix"), bool)
        and isinstance(incident.get("ts_unix"), int | float)
        and int(incident["ts_unix"]) < timestamp - INCIDENT_STATE_RETENTION_SECONDS
        for incident in incidents_state.values()
    )
    old_resolved_reviews = sum(
        isinstance(review, dict)
        and review.get("status") == "resolved"
        and not isinstance(
            review.get("resolved_at_unix") or review.get("updated_at_unix"), bool
        )
        and isinstance(
            review.get("resolved_at_unix") or review.get("updated_at_unix"),
            int | float,
        )
        and int(review.get("resolved_at_unix") or review.get("updated_at_unix"))
        < timestamp - RESOLVED_REVIEW_RETENTION_SECONDS
        for review in reviews_state.values()
    )
    candidate_count = (
        old_repair_events
        + old_notification_events
        + old_incidents
        + old_resolved_reviews
    )
    retention_preview = {
        "mode": "preview_only",
        "files_deleted": 0,
        "candidate_count": candidate_count,
        "action_required": candidate_count > 0,
        "repair_event_rows_older_than_90d": old_repair_events,
        "notification_event_rows_older_than_90d": old_notification_events,
        "incident_entries_older_than_30d": old_incidents,
        "resolved_reviews_older_than_90d": old_resolved_reviews,
    }

    reasons: list[str] = []
    if circuits["open_count"]:
        reasons.append("open_circuit")
    if pending_over_60m:
        reasons.append("pending_review_over_60m")
    if pending_over_15m and not pending_over_60m:
        reasons.append("pending_review_over_15m")
    if acknowledged_over_4h:
        reasons.append("acknowledged_review_over_4h")
    if windows["24h"]["failed"]:
        reasons.append("failed_auto_repair_in_24h")
    if source_errors:
        reasons.append("analytics_source_error")
    if circuits["open_count"] or pending_over_60m or source_errors:
        reliability_status = "critical"
    elif pending_over_15m or acknowledged_over_4h or windows["24h"]["failed"]:
        reliability_status = "degraded"
    else:
        reliability_status = "healthy"
    reliability = {"status": reliability_status, "reasons": reasons}

    codex_recommended = reliability_status != "healthy" or candidate_count > 0
    codex_deep_review = {
        "recommended": codex_recommended,
        "reasons": reasons + (["retention_preview_backlog"] if candidate_count else []),
        "mode": "read_only_input",
        "input_paths": [
            str(output_path)
            for output_path in (
                state_path,
                repair_events_path,
                notification_events_path,
            )
        ],
    }
    window_24h = windows["24h"]
    dashboard_summary = {
        "status": reliability_status,
        "auto_attempts_24h": window_24h["auto_attempts"],
        "success_rate_24h": window_24h["success_rate"],
        "mttr_mean_seconds_24h": window_24h["mttr_mean_seconds"],
        "open_circuits": circuits["open_count"],
        "open_reviews": review_metrics["open_count"],
        "pending_over_60m": pending_over_60m,
        "retention_candidates": candidate_count,
        "codex_review_recommended": codex_recommended,
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at_unix": timestamp,
        "generated_at": datetime.fromtimestamp(timestamp, UTC).isoformat(),
        "mode": "read_only",
        "reliability": reliability,
        "windows": windows,
        "reviews": review_metrics,
        "circuits": circuits,
        "retention_preview": retention_preview,
        "codex_deep_review": codex_deep_review,
        "dashboard_summary": dashboard_summary,
        "source_errors": source_errors,
    }


def refresh_analytics(
    *,
    state_path: Path,
    repair_events_path: Path,
    notification_events_path: Path,
    output_path: Path,
    now: int | None = None,
    min_interval_seconds: int = DEFAULT_REFRESH_INTERVAL_SECONDS,
    force: bool = False,
) -> dict[str, Any]:
    """Refresh the analytics snapshot under a non-blocking file lock.

    Args:
        state_path: Repair state source path.
        repair_events_path: Repair audit source path.
        notification_events_path: Notification audit source path.
        output_path: Destination for the derived snapshot.
        now: Optional Unix timestamp for deterministic tests.
        min_interval_seconds: Minimum interval between snapshot generations.
        force: Whether to ignore the minimum refresh interval.

    Returns:
        A written result with analytics, or a bounded skip reason.
    """
    timestamp = int(time.time()) if now is None else int(now)
    if output_path.exists() and not force:
        try:
            current = _load_json_object(output_path)
            generated_at = int(current.get("generated_at_unix", 0) or 0)
            if 0 <= timestamp - generated_at < max(0, min_interval_seconds):
                return {"status": "skipped", "reason": "snapshot_fresh"}
        except (OSError, ValueError, json.JSONDecodeError, TypeError):
            pass

    output_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = output_path.with_name(f"{output_path.name}.lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "skipped", "reason": "refresh_in_progress"}
        analytics = build_analytics(
            state_path=state_path,
            repair_events_path=repair_events_path,
            notification_events_path=notification_events_path,
            now=timestamp,
        )
        temporary_path = output_path.with_name(f".{output_path.name}.{os.getpid()}.tmp")
        temporary_path.write_text(
            json.dumps(analytics, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary_path, output_path)
    return {"status": "written", "analytics": analytics}


def main(argv: list[str] | None = None) -> int:
    """Run one bounded analytics refresh.

    Args:
        argv: Optional command-line arguments.

    Returns:
        Zero after a write or deterministic skip.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=STATE_PATH)
    parser.add_argument("--repair-events", type=Path, default=REPAIR_EVENTS_PATH)
    parser.add_argument(
        "--notification-events", type=Path, default=NOTIFICATION_EVENTS_PATH
    )
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    parser.add_argument(
        "--min-interval-seconds",
        type=int,
        default=DEFAULT_REFRESH_INTERVAL_SECONDS,
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if args.dry_run:
        result = build_analytics(
            state_path=args.state,
            repair_events_path=args.repair_events,
            notification_events_path=args.notification_events,
        )
    else:
        result = refresh_analytics(
            state_path=args.state,
            repair_events_path=args.repair_events,
            notification_events_path=args.notification_events,
            output_path=args.output,
            min_interval_seconds=max(0, args.min_interval_seconds),
            force=args.force,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
