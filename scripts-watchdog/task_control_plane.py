#!/usr/bin/env python3
"""Read-only Task Control Plane for DC-Agent operational work.

The module normalizes existing runtime sources without taking ownership of
their execution. Callers receive one stable task shape while launchd,
crontab, AstrBot Cron, Harness, and Knowledge Cycle keep their current
deterministic lifecycle responsibilities. Codex is always classified as an
optional advanced executor.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

KNOWLEDGE_GROUPS: dict[str, tuple[str, ...]] = {
    "mount": ("knowledge", "nas", "watchdog"),
    "kb_inbox": ("knowledge", "nas", "sync"),
    "kb_reconcile": ("knowledge", "nas", "sync"),
    "dc_memory": ("knowledge", "governance"),
    "obsidian_refs": ("knowledge", "governance"),
    "obsidian_governance_export": ("knowledge", "governance"),
    "obsidian_governance_export_tasks": ("knowledge", "governance"),
    "obsidian_governance_stale_scan": ("knowledge", "governance"),
    "obsidian_governance_import": ("knowledge", "governance"),
    "obsidian_governance_promote": ("knowledge", "governance"),
    "obsidian_governance_review_summary": ("knowledge", "governance"),
    "feishu_nas_workflow": ("knowledge", "nas", "sync"),
    "feishu_repair": ("knowledge", "nas", "sync"),
    "daily_full": ("knowledge", "nas", "night"),
}

KNOWLEDGE_REPLACEMENTS: dict[str, tuple[str, ...]] = {
    "feishu_repair": ("knowledge_cycle:feishu_nas_workflow",),
}

STATUS_MAP = {
    "ACTIVE": "ready",
    "PAUSED": "paused",
    "cancelled": "cancelled",
    "completed": "completed",
    "disabled": "disabled",
    "enabled": "ready",
    "fail": "failed",
    "failed": "failed",
    "in_progress": "running",
    "installed": "scheduled",
    "missing": "missing",
    "not-installed": "disabled",
    "ok": "completed",
    "paused": "paused",
    "pending": "pending",
    "review_required": "waiting",
    "running": "running",
    "scheduled": "scheduled",
    "stale_lock_removed": "failed",
    "timeout": "failed",
}


def _record(
    *,
    task_id: str,
    name: str,
    source: str,
    groups: tuple[str, ...] | list[str],
    category: str,
    native_status: str,
    status: str | None = None,
    enabled: bool,
    executor: str,
    executor_role: str = "deterministic_controller",
    control_mode: str = "managed",
    schedule: str = "",
    last_run_at: str = "",
    next_run_at: str = "",
    last_error: str = "",
    detail: str = "",
    authority_state: str = "authoritative",
    superseded_by: tuple[str, ...] | list[str] = (),
    group_pause_protected: bool = False,
    impact_level: str = "standard",
    impact_summary: str = "",
) -> dict[str, Any]:
    """Build one normalized operational task record.

    Args:
        task_id: Stable source-qualified task identifier.
        name: Human-readable task name.
        source: Runtime source that owns the task record.
        groups: Operational groups used for filtered views.
        category: Task purpose such as lifecycle, schedule, health, or work.
        native_status: Status text reported by the source.
        status: Optional normalized override.
        enabled: Whether the source currently permits execution.
        executor: Runtime that performs the work.
        executor_role: Authority classification for the executor.
        control_mode: Whether watchdogctl manages or only observes the task.
        schedule: Human-readable schedule when available.
        last_run_at: Latest known execution time.
        next_run_at: Next known execution time.
        last_error: Latest known error summary.
        detail: Concise source-specific evidence.
        authority_state: Whether this task still owns its operational role.
        superseded_by: Authoritative task IDs replacing a superseded task.
        group_pause_protected: Whether group pause must skip this task.
        impact_level: Operational impact classification for direct controls.
        impact_summary: User-facing summary of a direct control's impact.

    Returns:
        JSON-safe normalized task record.
    """
    normalized = status or STATUS_MAP.get(native_status, "unknown")
    return {
        "task_id": task_id,
        "name": name,
        "source": source,
        "groups": list(groups),
        "category": category,
        "status": normalized,
        "native_status": native_status,
        "enabled": enabled,
        "executor": executor,
        "executor_role": executor_role,
        "control_mode": control_mode,
        "schedule": schedule,
        "last_run_at": last_run_at,
        "next_run_at": next_run_at,
        "last_error": last_error,
        "detail": detail,
        "authority_state": authority_state,
        "superseded_by": list(superseded_by),
        "group_pause_protected": group_pause_protected,
        "impact_level": impact_level,
        "impact_summary": impact_summary,
    }


def collect_control_plane(
    legacy_status: dict[str, Any],
    *,
    dc_root: Path,
    group: str = "all",
) -> dict[str, Any]:
    """Collect the unified read-only Task Control Plane snapshot.

    Args:
        legacy_status: Existing watchdogctl status sections.
        dc_root: DC-Agent workspace root containing runtime state files.
        group: Optional operational group filter.

    Returns:
        Snapshot containing normalized tasks, counts, and isolated source errors.
    """
    tasks: list[dict[str, Any]] = []
    source_errors: dict[str, str] = {}

    for item in legacy_status.get("launchd", []):
        enabled_state = str(item.get("enabled_state") or "unknown")
        loaded_state = str(item.get("loaded_state") or "unknown")
        superseded_by = item.get("replacement_task_ids") or ()
        if superseded_by:
            status = (
                "migration_required"
                if loaded_state == "loaded" or enabled_state == "enabled"
                else "retired"
                if enabled_state == "disabled" and loaded_state == "not-loaded"
                else "unknown"
            )
        elif enabled_state == "disabled":
            status = "disabled"
        elif loaded_state == "loaded":
            status = "running"
        elif loaded_state == "not-loaded":
            status = "inactive"
        else:
            status = "unknown"
        tasks.append(
            _record(
                task_id=f"launchd:{item.get('key', '')}",
                name=str(item.get("description") or item.get("key") or "launchd"),
                source="launchd",
                groups=item.get("groups") or (),
                category="lifecycle",
                native_status=f"{enabled_state}/{loaded_state}",
                status=status,
                enabled=enabled_state != "disabled",
                executor="launchd",
                control_mode=(
                    "retire_only"
                    if superseded_by
                    else "managed"
                    if item.get("controllable", True)
                    else "read_only"
                ),
                schedule=str(item.get("schedule") or ""),
                detail=(
                    f"Superseded by {', '.join(str(task) for task in superseded_by)}"
                    if superseded_by
                    else str(item.get("label") or "")
                ),
                authority_state="superseded" if superseded_by else "authoritative",
                superseded_by=superseded_by,
                group_pause_protected=bool(item.get("group_pause_protected")),
                impact_level=str(item.get("impact_level") or "standard"),
                impact_summary=str(item.get("impact_summary") or ""),
            )
        )

    for item in legacy_status.get("cron", []):
        native_status = str(item.get("state") or "unknown")
        superseded_by = item.get("replacement_task_ids") or ()
        if superseded_by:
            status = (
                "migration_required"
                if native_status == "installed"
                else "retired"
                if native_status == "not-installed"
                else "unknown"
            )
        else:
            status = None
        tasks.append(
            _record(
                task_id=f"crontab:{item.get('key', '')}",
                name=str(item.get("description") or item.get("key") or "crontab"),
                source="crontab",
                groups=item.get("groups") or (),
                category="schedule",
                native_status=native_status,
                status=status,
                enabled=native_status == "installed",
                executor="crontab",
                control_mode=(
                    "retire_only"
                    if superseded_by
                    else "managed"
                    if item.get("controllable", True)
                    else "read_only"
                ),
                detail=(
                    f"Superseded by {', '.join(str(task) for task in superseded_by)}"
                    if superseded_by
                    else ""
                ),
                authority_state="superseded" if superseded_by else "authoritative",
                superseded_by=superseded_by,
                group_pause_protected=bool(item.get("group_pause_protected")),
                impact_level=str(item.get("impact_level") or "standard"),
                impact_summary=str(item.get("impact_summary") or ""),
            )
        )

    for item in legacy_status.get("codex", []):
        native_status = str(item.get("status") or "unknown")
        tasks.append(
            _record(
                task_id=f"codex_automation:{item.get('key', '')}",
                name=str(item.get("description") or item.get("key") or "Codex"),
                source="codex_automation",
                groups=item.get("groups") or (),
                category="advanced_work",
                native_status=native_status,
                enabled=native_status == "ACTIVE",
                executor="codex",
                executor_role="advanced_executor",
                control_mode="pause_only",
                detail="Optional capability; scheduling cannot be resumed",
                authority_state="non_authoritative",
                impact_level=str(item.get("impact_level") or "standard"),
                impact_summary=str(item.get("impact_summary") or ""),
            )
        )

    for item in legacy_status.get("probes", []):
        native_status = str(item.get("state") or "unknown")
        tasks.append(
            _record(
                task_id=f"watchdog_probe:{item.get('key', '')}",
                name=str(item.get("key") or "watchdog probe"),
                source="watchdog_probe",
                groups=item.get("groups") or (),
                category="health",
                native_status=native_status,
                enabled=native_status == "enabled",
                executor="dc-watchdog",
                control_mode="read_only",
            )
        )

    for item in (legacy_status.get("repair") or {}).get("services", []):
        if not isinstance(item, dict):
            continue
        service = str(item.get("service") or "unknown")
        native_status = str(item.get("state") or "idle")
        detail = (
            f"attempts={item.get('attempt_count_window', 0)}; "
            f"remaining={item.get('attempts_remaining', 0)}; "
            f"failures={item.get('consecutive_failures', 0)}; "
            f"circuit_remaining_seconds={item.get('circuit_remaining_seconds', 0)}; "
            f"last_incident={item.get('last_incident_id', '')}"
        )
        tasks.append(
            _record(
                task_id=f"watchdog_repair:{service}",
                name=f"{service} self-repair guard",
                source="watchdog_repair",
                groups=item.get("groups") or ("watchdog", "repair"),
                category="repair_guard",
                native_status=native_status,
                status=native_status,
                enabled=True,
                executor="repair_engine",
                control_mode="read_only",
                last_run_at=str(item.get("last_attempt_at") or ""),
                last_error=(
                    detail if native_status in {"failed", "circuit_open"} else ""
                ),
                detail=detail,
                authority_state="authoritative",
            )
        )

    for item in (legacy_status.get("repair") or {}).get("reviews", []):
        if not isinstance(item, dict):
            continue
        incident_id = str(item.get("incident_id") or "unknown")
        status = str(item.get("status") or "pending")
        tasks.append(
            _record(
                task_id=f"watchdog_repair_review:{incident_id}",
                name=f"Repair review {incident_id}",
                source="watchdog_repair_review",
                groups=item.get("groups") or ("watchdog", "repair"),
                category="review",
                native_status=status,
                status=status,
                enabled=status != "resolved",
                executor="local_operator",
                executor_role="human_operator",
                control_mode="review_plan",
                last_run_at=(
                    datetime.fromtimestamp(
                        int(item.get("updated_at_unix", 0) or 0), UTC
                    ).isoformat()
                    if item.get("updated_at_unix")
                    else ""
                ),
                last_error=(
                    str(item.get("reason") or "") if status != "resolved" else ""
                ),
                detail=(
                    f"service={item.get('service', '')}; "
                    f"risk={item.get('risk', 'high')}; "
                    f"summary={item.get('summary', '')}"
                ),
                authority_state="authoritative",
                impact_level="review",
                impact_summary=(
                    "Review metadata only; no runtime repair command is exposed."
                ),
            )
        )

    knowledge_path = dc_root / "data" / "watchdog" / "knowledge_cycle_state.json"
    if knowledge_path.exists():
        try:
            knowledge_state = json.loads(knowledge_path.read_text(encoding="utf-8"))
            paused = (
                dc_root / "data" / "watchdog" / "feishu_cloud_workflow.pause"
            ).exists()
            for step, raw in (knowledge_state.get("steps") or {}).items():
                if not isinstance(raw, dict):
                    continue
                native_status = str(raw.get("status") or "unknown")
                superseded_by = KNOWLEDGE_REPLACEMENTS.get(step, ())
                if superseded_by:
                    status = (
                        "migration_required"
                        if native_status
                        in {"in_progress", "pending", "running", "scheduled"}
                        else "retired"
                    )
                else:
                    status = (
                        "paused"
                        if step == "feishu_nas_workflow" and paused
                        else STATUS_MAP.get(native_status, "unknown")
                    )
                detail_parts = []
                if raw.get("reason"):
                    detail_parts.append(str(raw["reason"]))
                if raw.get("exit_code") is not None:
                    detail_parts.append(f"exit_code={raw['exit_code']}")
                if raw.get("duration_sec") is not None:
                    detail_parts.append(f"duration_sec={raw['duration_sec']}")
                tasks.append(
                    _record(
                        task_id=f"knowledge_cycle:{step}",
                        name=step,
                        source="knowledge_cycle",
                        groups=KNOWLEDGE_GROUPS.get(step, ("knowledge",)),
                        category="work",
                        native_status=native_status,
                        status=status,
                        enabled=not superseded_by and native_status != "disabled",
                        executor="knowledge_cycle",
                        control_mode="retire_only" if superseded_by else "read_only",
                        last_run_at=str(
                            raw.get("run_finished_at")
                            or raw.get("last_started_at")
                            or ""
                        ),
                        last_error=(
                            str(raw.get("reason") or "") if status == "failed" else ""
                        ),
                        detail=(
                            f"Superseded by {', '.join(superseded_by)}"
                            if superseded_by
                            else "; ".join(detail_parts)
                        ),
                        authority_state=(
                            "superseded" if superseded_by else "authoritative"
                        ),
                        superseded_by=superseded_by,
                    )
                )
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            source_errors["knowledge_cycle"] = str(exc)
    else:
        source_errors["knowledge_cycle"] = "missing state file"

    cron_db = dc_root / "data" / "data_v4.db"
    if cron_db.exists():
        try:
            with sqlite3.connect(f"file:{cron_db}?mode=ro", uri=True) as db:
                db.row_factory = sqlite3.Row
                rows = db.execute(
                    """
                    SELECT job_id, name, job_type, cron_expression, enabled,
                           status, last_run_at, next_run_time, last_error
                    FROM cron_jobs
                    ORDER BY updated_at DESC
                    LIMIT 100
                    """
                ).fetchall()
            for row in rows:
                native_status = str(row["status"] or "unknown")
                enabled = bool(row["enabled"])
                status = (
                    STATUS_MAP.get(native_status, "unknown") if enabled else "disabled"
                )
                tasks.append(
                    _record(
                        task_id=f"astrbot_cron:{row['job_id']}",
                        name=str(row["name"] or row["job_id"]),
                        source="astrbot_cron",
                        groups=("agent",),
                        category="schedule",
                        native_status=native_status,
                        status=status,
                        enabled=enabled,
                        executor=(
                            "astrbot_agent"
                            if row["job_type"] == "active_agent"
                            else "python_handler"
                        ),
                        control_mode="read_only",
                        schedule=str(row["cron_expression"] or ""),
                        last_run_at=str(row["last_run_at"] or ""),
                        next_run_at=str(row["next_run_time"] or ""),
                        last_error=str(row["last_error"] or ""),
                    )
                )
        except (sqlite3.Error, OSError) as exc:
            source_errors["astrbot_cron"] = str(exc)
    else:
        source_errors["astrbot_cron"] = "missing database"

    harness_db = dc_root / "data" / "harness_tasks.db"
    if harness_db.exists():
        try:
            with sqlite3.connect(f"file:{harness_db}?mode=ro", uri=True) as db:
                db.row_factory = sqlite3.Row
                rows = db.execute(
                    """
                    SELECT task_id, title, domain, status, updated_at
                    FROM harness_tasks
                    ORDER BY updated_at DESC
                    LIMIT 100
                    """
                ).fetchall()
            for row in rows:
                native_status = str(row["status"] or "unknown")
                tasks.append(
                    _record(
                        task_id=f"harness:{row['task_id']}",
                        name=str(row["title"] or row["task_id"]),
                        source="harness",
                        groups=("harness", str(row["domain"] or "general")),
                        category="work",
                        native_status=native_status,
                        enabled=native_status
                        not in {
                            "cancelled",
                            "completed",
                            "failed",
                        },
                        executor="harness",
                        control_mode="read_only",
                        last_run_at=str(row["updated_at"] or ""),
                    )
                )
        except (sqlite3.Error, OSError) as exc:
            source_errors["harness"] = str(exc)
    else:
        source_errors["harness"] = "missing database"

    if group != "all":
        tasks = [task for task in tasks if group in task["groups"]]
    tasks.sort(key=lambda task: (task["source"], task["name"], task["task_id"]))
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "mode": "read_only",
        "group": group,
        "tasks": tasks,
        "task_count": len(tasks),
        "source_counts": dict(
            sorted(Counter(task["source"] for task in tasks).items())
        ),
        "status_counts": dict(
            sorted(Counter(task["status"] for task in tasks).items())
        ),
        "source_errors": source_errors,
    }
