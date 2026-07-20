from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ANALYTICS = Path("scripts-watchdog/repair_analytics.py")
REPAIR_ENGINE = Path("scripts-watchdog/repair_engine.py")
WATCHDOG_SCRIPT = Path("scripts-watchdog/dc-watchdog.sh")
DASHBOARD_ASSET = Path("data/plugins/system_entries/dc-dashboard-quick-entries.js")


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(f"{json.dumps(row)}\n" for row in rows),
        encoding="utf-8",
    )


def test_analytics_aggregates_slo_sla_circuits_and_retention_without_mutation(
    tmp_path,
) -> None:
    module = _load_module(ANALYTICS, "watchdog_repair_analytics")
    now = 10_000_000
    state = tmp_path / "repair_state.json"
    repair_events = tmp_path / "repairs.jsonl"
    notification_events = tmp_path / "review_notifications.jsonl"
    output = tmp_path / "repair_analytics.json"
    state.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "incidents": {
                    "recent": {"status": "repaired", "ts_unix": now - 100},
                    "old": {"status": "failed", "ts_unix": now - 2_592_001},
                },
                "services": {
                    "astrbot_api": {
                        "attempts": [now - 60],
                        "consecutive_failures": 2,
                        "circuit_open_until": now + 600,
                        "last_outcome": "failed",
                        "last_incident_id": "recent",
                    }
                },
                "reviews": {
                    "pending": {
                        "service": "assistant_chat_health",
                        "status": "pending",
                        "created_at_unix": now - 3_601,
                        "updated_at_unix": now - 3_601,
                    },
                    "acknowledged": {
                        "service": "astrbot_api",
                        "status": "acknowledged",
                        "created_at_unix": now - 20_000,
                        "updated_at_unix": now - 18_000,
                        "acknowledged_at_unix": now - 18_000,
                    },
                    "resolved-old": {
                        "service": "astrbot_api",
                        "status": "resolved",
                        "created_at_unix": now - 9_000_000,
                        "updated_at_unix": now - 8_000_001,
                        "resolved_at_unix": now - 8_000_001,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    _write_jsonl(
        repair_events,
        [
            {
                "schema_version": 1,
                "ts_unix": now - 100,
                "incident_id": "repair-ok",
                "service": "astrbot_api",
                "action_id": "restart_managed_service",
                "status": "repaired",
                "duration_seconds": 120,
                "stdout": "must-not-leak",
            },
            {
                "schema_version": 1,
                "ts_unix": now - 200,
                "incident_id": "repair-failed",
                "service": "hermes_gateway",
                "action_id": "restart_managed_service",
                "status": "failed",
                "duration_seconds": 300,
            },
            {
                "schema_version": 1,
                "ts_unix": now - 8_000_001,
                "incident_id": "repair-old",
                "service": "astrbot_api",
                "action_id": "restart_managed_service",
                "status": "repaired",
                "duration_seconds": 30,
            },
        ],
    )
    _write_jsonl(
        notification_events,
        [
            {"ts_unix": now - 300, "stage": "pending_15m", "status": "sent"},
            {"ts_unix": now - 8_000_001, "stage": "created", "status": "sent"},
        ],
    )
    source_contents = {
        path: path.read_bytes() for path in (state, repair_events, notification_events)
    }

    result = module.refresh_analytics(
        state_path=state,
        repair_events_path=repair_events,
        notification_events_path=notification_events,
        output_path=output,
        now=now,
        force=True,
    )
    analytics = result["analytics"]

    assert result["status"] == "written"
    assert analytics["reliability"]["status"] == "critical"
    assert analytics["windows"]["24h"] == {
        "result_count": 2,
        "auto_attempts": 2,
        "repaired": 1,
        "failed": 1,
        "success_rate": 0.5,
        "mttr_sample_count": 1,
        "mttr_mean_seconds": 120,
        "mttr_p95_seconds": 120,
        "notification_count": 1,
    }
    assert analytics["windows"]["7d"] == analytics["windows"]["24h"]
    assert analytics["reviews"]["open_count"] == 2
    assert analytics["reviews"]["pending_over_60m"] == 1
    assert analytics["reviews"]["acknowledged_over_4h"] == 1
    assert analytics["circuits"]["open_count"] == 1
    assert analytics["retention_preview"]["mode"] == "preview_only"
    assert analytics["retention_preview"]["files_deleted"] == 0
    assert analytics["retention_preview"]["candidate_count"] == 4
    assert analytics["codex_deep_review"]["recommended"] is True
    assert "must-not-leak" not in output.read_text(encoding="utf-8")
    for path, content in source_contents.items():
        assert path.read_bytes() == content


def test_analytics_refresh_skips_recent_snapshot(tmp_path) -> None:
    module = _load_module(ANALYTICS, "watchdog_repair_analytics_refresh")
    state = tmp_path / "repair_state.json"
    repairs = tmp_path / "repairs.jsonl"
    notifications = tmp_path / "notifications.jsonl"
    output = tmp_path / "repair_analytics.json"
    state.write_text(
        json.dumps(
            {"schema_version": 1, "incidents": {}, "services": {}, "reviews": {}}
        ),
        encoding="utf-8",
    )

    first = module.refresh_analytics(
        state_path=state,
        repair_events_path=repairs,
        notification_events_path=notifications,
        output_path=output,
        now=20_000,
        force=True,
    )
    initial = output.read_bytes()
    second = module.refresh_analytics(
        state_path=state,
        repair_events_path=repairs,
        notification_events_path=notifications,
        output_path=output,
        now=20_100,
        min_interval_seconds=300,
    )

    assert first["status"] == "written"
    assert second == {"status": "skipped", "reason": "snapshot_fresh"}
    assert output.read_bytes() == initial


def test_repair_status_projects_only_bounded_analytics_fields(tmp_path) -> None:
    analytics_module = _load_module(ANALYTICS, "watchdog_analytics_projection_source")
    engine = _load_module(REPAIR_ENGINE, "watchdog_analytics_projection_engine")
    state = tmp_path / "repair_state.json"
    repairs = tmp_path / "repairs.jsonl"
    notifications = tmp_path / "notifications.jsonl"
    analytics = tmp_path / "repair_analytics.json"
    state.write_text(
        json.dumps(
            {"schema_version": 1, "incidents": {}, "services": {}, "reviews": {}}
        ),
        encoding="utf-8",
    )
    payload = analytics_module.build_analytics(
        state_path=state,
        repair_events_path=repairs,
        notification_events_path=notifications,
        now=30_000,
    )
    payload["untrusted_extra"] = "must-not-leak"
    payload["dashboard_summary"]["untrusted_extra"] = "must-not-leak"
    analytics.write_text(json.dumps(payload), encoding="utf-8")

    snapshot = engine.collect_repair_status(
        state_path=state,
        events_path=repairs,
        analytics_path=analytics,
        now=30_001,
    )

    assert snapshot["analytics"]["dashboard_summary"]["status"] == "healthy"
    assert "must-not-leak" not in json.dumps(snapshot)


def test_watchdog_and_dashboard_expose_repair_analytics() -> None:
    watchdog_source = WATCHDOG_SCRIPT.read_text(encoding="utf-8")
    dashboard_source = DASHBOARD_ASSET.read_text(encoding="utf-8")

    assert "repair_analytics.py" in watchdog_source
    assert "nohup" in watchdog_source
    assert "自愈可靠性指标" in dashboard_source
    assert "codex_review_recommended" in dashboard_source
