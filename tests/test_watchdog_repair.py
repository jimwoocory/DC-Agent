from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from harness.evaluator.kb_import_contract import load_contract, validate_contract

CONTRACT = Path("harness/contracts/watchdog_repair_closure.json")
REPAIR_ENGINE = Path("scripts-watchdog/repair_engine.py")


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _write_incident(
    path: Path,
    *,
    incident_id: str,
    service: str = "astrbot_api",
    probe: str = "http:http://127.0.0.1:6185/api/stat/start-time",
) -> None:
    path.write_text(
        json.dumps(
            {
                "incident_id": incident_id,
                "service": service,
                "probe": probe,
                "cur_status": "fail:000",
            }
        ),
        encoding="utf-8",
    )


def _write_proposal(
    path: Path,
    *,
    incident_id: str,
    service: str = "astrbot_api",
    action_id: str = "restart_managed_service",
    confidence: float = 0.9,
    risk_hint: str = "low",
) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "incident_id": incident_id,
                "service": service,
                "summary": "The managed process is unavailable.",
                "confidence": confidence,
                "action_id": action_id,
                "reason": "A bounded restart is likely to restore the listener.",
                "risk_hint": risk_hint,
            }
        ),
        encoding="utf-8",
    )


def test_watchdog_repair_contract_is_valid() -> None:
    contract = load_contract(CONTRACT)

    assert validate_contract(contract) == []


def test_proposal_schema_never_accepts_model_supplied_commands() -> None:
    module = _load_module(REPAIR_ENGINE, "watchdog_repair_schema")

    schema = module.proposal_schema()
    properties = schema["properties"]

    assert schema["additionalProperties"] is False
    assert "command" not in properties
    assert properties["action_id"]["enum"] == [
        "observe_only",
        "restart_managed_service",
        "manual_intervention",
    ]


def test_diagnosis_bundle_schema_combines_report_and_bounded_proposal() -> None:
    module = _load_module(REPAIR_ENGINE, "watchdog_repair_bundle_schema")

    schema = module.diagnosis_bundle_schema()

    assert schema["additionalProperties"] is False
    assert schema["required"] == ["report_markdown", "proposal"]
    assert schema["properties"]["proposal"]["additionalProperties"] is False
    assert "command" not in schema["properties"]["proposal"]["properties"]


def test_diagnosis_bundle_is_split_into_report_and_proposal(tmp_path) -> None:
    module = _load_module(REPAIR_ENGINE, "watchdog_repair_bundle_split")
    bundle = tmp_path / "bundle.json"
    report = tmp_path / "report.md"
    proposal = tmp_path / "proposal.json"
    proposal_payload = {
        "schema_version": 1,
        "incident_id": "bundle-1",
        "service": "astrbot_api",
        "summary": "The listener is unavailable.",
        "confidence": 0.9,
        "action_id": "restart_managed_service",
        "reason": "The process endpoint is down.",
        "risk_hint": "low",
    }
    bundle.write_text(
        json.dumps(
            {
                "report_markdown": "## 1. 结论\n服务当前不可用。",
                "proposal": proposal_payload,
            }
        ),
        encoding="utf-8",
    )

    module.split_diagnosis_bundle(
        bundle_path=bundle,
        report_path=report,
        proposal_path=proposal,
    )

    assert report.read_text(encoding="utf-8") == "## 1. 结论\n服务当前不可用。\n"
    assert json.loads(proposal.read_text(encoding="utf-8")) == proposal_payload


def test_policy_recomputes_risk_and_uses_fixed_restart_command(tmp_path) -> None:
    module = _load_module(REPAIR_ENGINE, "watchdog_repair_policy")
    incident = {
        "incident_id": "101",
        "service": "astrbot_api",
        "probe": "http:http://127.0.0.1:6185/api/stat/start-time",
        "cur_status": "fail:000",
    }
    proposal = {
        "schema_version": 1,
        "incident_id": "101",
        "service": "astrbot_api",
        "summary": "down",
        "confidence": 0.95,
        "action_id": "restart_managed_service",
        "reason": "restart",
        "risk_hint": "high",
    }

    decision = module.decide_repair(incident, proposal, dc_root=tmp_path)

    assert decision.allowed is True
    assert decision.risk == "low"
    assert decision.action is not None
    assert decision.action.command == (
        str(tmp_path / "scripts-tools" / "safe_restart.sh"),
        "astrbot",
    )
    assert "high" not in decision.reason


def test_manual_and_mismatched_proposals_never_execute(tmp_path) -> None:
    module = _load_module(REPAIR_ENGINE, "watchdog_repair_denial")
    incident = tmp_path / "incident.json"
    proposal = tmp_path / "proposal.json"
    state = tmp_path / "state.json"
    result = tmp_path / "result.json"
    events = tmp_path / "events.jsonl"
    _write_incident(incident, incident_id="102")
    _write_proposal(
        proposal,
        incident_id="wrong",
        action_id="manual_intervention",
        risk_hint="low",
    )
    calls: list[tuple[str, ...]] = []

    def executor(command: tuple[str, ...], **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    repair = module.run_repair(
        incident_path=incident,
        proposal_path=proposal,
        state_path=state,
        result_path=result,
        events_path=events,
        dc_root=tmp_path,
        executor=executor,
        verifier=lambda _incident: (True, "healthy"),
        now=1_000,
    )

    assert repair["status"] == "denied"
    assert repair["reason"] == "proposal_incident_mismatch"
    assert calls == []


def test_successful_repair_is_verified_recorded_and_idempotent(tmp_path) -> None:
    module = _load_module(REPAIR_ENGINE, "watchdog_repair_success")
    incident = tmp_path / "incident.json"
    proposal = tmp_path / "proposal.json"
    state = tmp_path / "state.json"
    result = tmp_path / "result.json"
    second_result = tmp_path / "result-second.json"
    events = tmp_path / "events.jsonl"
    _write_incident(incident, incident_id="103")
    _write_proposal(proposal, incident_id="103")
    calls: list[tuple[str, ...]] = []

    def executor(command: tuple[str, ...], **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="secret", stderr="")

    verification_results = iter(((False, "http_unavailable"), (True, "http_200")))
    first = module.run_repair(
        incident_path=incident,
        proposal_path=proposal,
        state_path=state,
        result_path=result,
        events_path=events,
        dc_root=tmp_path,
        executor=executor,
        verifier=lambda _incident: next(verification_results),
        now=2_000,
    )
    second = module.run_repair(
        incident_path=incident,
        proposal_path=proposal,
        state_path=state,
        result_path=second_result,
        events_path=events,
        dc_root=tmp_path,
        executor=executor,
        verifier=lambda _incident: (True, "http_200"),
        now=2_001,
    )

    assert first["status"] == "repaired"
    assert first["preflight_verification"] == {
        "ok": False,
        "detail": "http_unavailable",
    }
    assert first["verification"] == {"ok": True, "detail": "http_200"}
    assert first["rollback"] == "not_required_non_persistent_action"
    assert "secret" not in result.read_text(encoding="utf-8")
    assert second["status"] == "denied"
    assert second["reason"] == "incident_already_handled"
    assert len(calls) == 1
    event_rows = events.read_text(encoding="utf-8").splitlines()
    assert len(event_rows) == 2


def test_successful_repair_records_incident_to_verification_duration(tmp_path) -> None:
    module = _load_module(REPAIR_ENGINE, "watchdog_repair_duration")
    incident = tmp_path / "incident.json"
    proposal = tmp_path / "proposal.json"
    state = tmp_path / "state.json"
    result = tmp_path / "result.json"
    events = tmp_path / "events.jsonl"
    incident.write_text(
        json.dumps(
            {
                "incident_id": "duration-1",
                "service": "astrbot_api",
                "probe": "http:http://127.0.0.1:6185/api/stat/start-time",
                "cur_status": "fail:000",
                "ts_unix": 1_900,
            }
        ),
        encoding="utf-8",
    )
    _write_proposal(proposal, incident_id="duration-1")
    verification_results = iter(((False, "http_unavailable"), (True, "http_200")))

    repair = module.run_repair(
        incident_path=incident,
        proposal_path=proposal,
        state_path=state,
        result_path=result,
        events_path=events,
        dc_root=tmp_path,
        executor=lambda command, **_kwargs: subprocess.CompletedProcess(command, 0),
        verifier=lambda _incident: next(verification_results),
        now=2_000,
    )

    assert repair["status"] == "repaired"
    assert repair["incident_started_at_unix"] == 1_900
    assert repair["completed_at_unix"] == 2_000
    assert repair["duration_seconds"] == 100


def test_repair_is_cancelled_when_service_recovered_before_execution(tmp_path) -> None:
    module = _load_module(REPAIR_ENGINE, "watchdog_repair_preflight")
    incident = tmp_path / "incident.json"
    proposal = tmp_path / "proposal.json"
    state = tmp_path / "state.json"
    result = tmp_path / "result.json"
    events = tmp_path / "events.jsonl"
    _write_incident(incident, incident_id="104")
    _write_proposal(proposal, incident_id="104")
    calls: list[tuple[str, ...]] = []

    def executor(command: tuple[str, ...], **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    repair = module.run_repair(
        incident_path=incident,
        proposal_path=proposal,
        state_path=state,
        result_path=result,
        events_path=events,
        dc_root=tmp_path,
        executor=executor,
        verifier=lambda _incident: (True, "http_200"),
        now=2_100,
    )

    assert repair["status"] == "no_action"
    assert repair["reason"] == "service_already_recovered"
    assert repair["preflight_verification"] == {"ok": True, "detail": "http_200"}
    assert calls == []
    assert json.loads(state.read_text(encoding="utf-8"))["services"] == {}


def test_repair_status_reports_budget_circuit_and_sanitized_results(tmp_path) -> None:
    module = _load_module(REPAIR_ENGINE, "watchdog_repair_status")
    state = tmp_path / "repair_state.json"
    events = tmp_path / "repairs.jsonl"
    state.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "incidents": {},
                "services": {
                    "astrbot_api": {
                        "attempts": [1_000, 4_500, 4_900],
                        "consecutive_failures": 2,
                        "circuit_open_until": 7_000,
                        "last_outcome": "failed",
                        "last_incident_id": "incident-9",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    events.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "ts_unix": 4_900,
                "incident_id": "incident-9",
                "service": "astrbot_api",
                "action_id": "restart_managed_service",
                "status": "failed",
                "reason": "repair_verification_failed",
                "risk": "low",
                "attempt_count": 2,
                "verification": {"ok": False, "detail": "http_unavailable"},
                "stdout": "must-not-leak",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    snapshot = module.collect_repair_status(
        state_path=state,
        events_path=events,
        now=5_000,
    )

    service = snapshot["services"][0]
    assert snapshot["mode"] == "read_only"
    assert service["service"] == "astrbot_api"
    assert service["state"] == "circuit_open"
    assert service["attempt_count_window"] == 2
    assert service["attempts_remaining"] == 0
    assert service["circuit_remaining_seconds"] == 2_000
    assert service["groups"] == ["watchdog", "repair", "astrbot"]
    assert snapshot["recent_results"][0]["verification_detail"] == ("http_unavailable")
    assert "must-not-leak" not in json.dumps(snapshot)


def test_repair_status_missing_files_is_empty_and_read_only(tmp_path) -> None:
    module = _load_module(REPAIR_ENGINE, "watchdog_repair_status_empty")

    snapshot = module.collect_repair_status(
        state_path=tmp_path / "missing-state.json",
        events_path=tmp_path / "missing-events.jsonl",
        now=5_000,
    )

    assert snapshot["state_status"] == "empty"
    assert snapshot["services"] == []
    assert snapshot["recent_results"] == []
    assert snapshot["source_errors"] == {}
    assert list(tmp_path.iterdir()) == []


def test_repair_status_invalid_sources_are_reported_without_mutation(tmp_path) -> None:
    module = _load_module(REPAIR_ENGINE, "watchdog_repair_status_invalid")
    state = tmp_path / "repair_state.json"
    events = tmp_path / "repairs.jsonl"
    state.write_text("{invalid", encoding="utf-8")
    events.write_text("not-json\n", encoding="utf-8")
    original_state = state.read_text(encoding="utf-8")
    original_events = events.read_text(encoding="utf-8")

    snapshot = module.collect_repair_status(
        state_path=state,
        events_path=events,
        now=5_000,
    )

    assert snapshot["state_status"] == "invalid"
    assert set(snapshot["source_errors"]) == {"repair_state", "repair_events"}
    assert state.read_text(encoding="utf-8") == original_state
    assert events.read_text(encoding="utf-8") == original_events


def test_manual_intervention_creates_bounded_review_queue_item(tmp_path) -> None:
    module = _load_module(REPAIR_ENGINE, "watchdog_repair_manual_review")
    incident = tmp_path / "incident.json"
    proposal = tmp_path / "proposal.json"
    state = tmp_path / "state.json"
    result = tmp_path / "result.json"
    events = tmp_path / "events.jsonl"
    _write_incident(incident, incident_id="manual-1", service="assistant_chat_health")
    _write_proposal(
        proposal,
        incident_id="manual-1",
        service="assistant_chat_health",
        action_id="manual_intervention",
        confidence=0.9,
        risk_hint="high",
    )
    calls: list[tuple[str, ...]] = []

    repair = module.run_repair(
        incident_path=incident,
        proposal_path=proposal,
        state_path=state,
        result_path=result,
        events_path=events,
        dc_root=tmp_path,
        executor=lambda command, **_kwargs: calls.append(command),
        verifier=lambda _incident: (False, "still_down"),
        now=6_000,
    )

    saved = json.loads(state.read_text(encoding="utf-8"))
    review = saved["reviews"]["manual-1"]
    assert repair["status"] == "review_required"
    assert repair["review_status"] == "pending"
    assert review["status"] == "pending"
    assert review["service"] == "assistant_chat_health"
    assert review["summary"] == "The managed process is unavailable."
    assert "command" not in review
    assert calls == []
    snapshot = module.collect_repair_status(
        state_path=state,
        events_path=events,
        now=6_001,
    )
    assert snapshot["pending_review_count"] == 1
    assert snapshot["reviews"][0]["incident_id"] == "manual-1"


def test_recovered_service_does_not_create_manual_review(tmp_path) -> None:
    module = _load_module(REPAIR_ENGINE, "watchdog_repair_recovered_manual")
    incident = tmp_path / "incident.json"
    proposal = tmp_path / "proposal.json"
    state = tmp_path / "state.json"
    result = tmp_path / "result.json"
    events = tmp_path / "events.jsonl"
    _write_incident(incident, incident_id="manual-recovered")
    _write_proposal(
        proposal,
        incident_id="manual-recovered",
        action_id="manual_intervention",
        confidence=0.9,
        risk_hint="high",
    )

    repair = module.run_repair(
        incident_path=incident,
        proposal_path=proposal,
        state_path=state,
        result_path=result,
        events_path=events,
        dc_root=tmp_path,
        verifier=lambda _incident: (True, "http_200"),
        now=6_100,
    )

    saved = json.loads(state.read_text(encoding="utf-8"))
    assert repair["status"] == "no_action"
    assert repair["reason"] == "service_already_recovered"
    assert repair["preflight_verification"] == {"ok": True, "detail": "http_200"}
    assert saved["reviews"] == {}
    assert saved["incidents"]["manual-recovered"] == {
        "status": "no_action",
        "reason": "service_already_recovered",
        "ts_unix": 6_100,
    }


def test_review_control_plan_enforces_exact_atomic_state_transitions(tmp_path) -> None:
    module = _load_module(REPAIR_ENGINE, "watchdog_repair_review_plan")
    state = tmp_path / "state.json"
    state.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "incidents": {},
                "services": {"astrbot_api": {"attempts": [900]}},
                "reviews": {
                    "manual-2": {
                        "service": "astrbot_api",
                        "status": "pending",
                        "created_at_unix": 900,
                        "updated_at_unix": 900,
                        "summary": "Configuration evidence needs review.",
                        "reason": "No fixed automatic action is authorized.",
                        "risk": "high",
                        "action_id": "manual_intervention",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    acknowledge = module.build_review_control_plan(
        state_path=state,
        incident_id="manual-2",
        operation="acknowledge",
        issued_at=1_000,
    )
    with pytest.raises(ValueError, match="confirmation is required"):
        module.apply_review_control_plan(
            state_path=state,
            incident_id="manual-2",
            operation="acknowledge",
            confirm_plan=None,
            now=1_001,
        )

    applied = module.apply_review_control_plan(
        state_path=state,
        incident_id="manual-2",
        operation="acknowledge",
        confirm_plan=acknowledge["plan_id"],
        now=1_001,
    )

    assert applied["from_status"] == "pending"
    assert applied["to_status"] == "acknowledged"
    acknowledged_state = json.loads(state.read_text(encoding="utf-8"))
    assert acknowledged_state["reviews"]["manual-2"]["status"] == "acknowledged"
    assert acknowledged_state["services"] == {"astrbot_api": {"attempts": [900]}}

    resolve = module.build_review_control_plan(
        state_path=state,
        incident_id="manual-2",
        operation="resolve",
        issued_at=1_010,
    )
    module.apply_review_control_plan(
        state_path=state,
        incident_id="manual-2",
        operation="resolve",
        confirm_plan=resolve["plan_id"],
        now=1_011,
    )

    resolved_state = json.loads(state.read_text(encoding="utf-8"))
    assert resolved_state["reviews"]["manual-2"]["status"] == "resolved"
    assert resolved_state["reviews"]["manual-2"]["resolved_at_unix"] == 1_011


def test_review_control_plan_rejects_expired_or_stale_confirmation(tmp_path) -> None:
    module = _load_module(REPAIR_ENGINE, "watchdog_repair_review_stale")
    state = tmp_path / "state.json"
    payload = {
        "schema_version": 1,
        "incidents": {},
        "services": {},
        "reviews": {
            "manual-3": {
                "service": "astrbot_api",
                "status": "pending",
                "created_at_unix": 900,
                "updated_at_unix": 900,
                "summary": "Review needed.",
                "reason": "Unknown root cause.",
                "risk": "high",
                "action_id": "manual_intervention",
            }
        },
    }
    state.write_text(json.dumps(payload), encoding="utf-8")
    plan = module.build_review_control_plan(
        state_path=state,
        incident_id="manual-3",
        operation="acknowledge",
        issued_at=1_000,
    )

    with pytest.raises(ValueError, match="expired"):
        module.apply_review_control_plan(
            state_path=state,
            incident_id="manual-3",
            operation="acknowledge",
            confirm_plan=plan["plan_id"],
            now=1_121,
        )

    payload["reviews"]["manual-3"]["updated_at_unix"] = 999
    state.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="stale"):
        module.apply_review_control_plan(
            state_path=state,
            incident_id="manual-3",
            operation="acknowledge",
            confirm_plan=plan["plan_id"],
            now=1_001,
        )


def test_failed_verification_opens_circuit_and_stops_retrying(tmp_path) -> None:
    module = _load_module(REPAIR_ENGINE, "watchdog_repair_circuit")
    state = tmp_path / "state.json"
    events = tmp_path / "events.jsonl"
    calls: list[tuple[str, ...]] = []

    def executor(command: tuple[str, ...], **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    results = []
    for index, now in enumerate((3_000, 3_100, 3_200), start=1):
        incident = tmp_path / f"incident-{index}.json"
        proposal = tmp_path / f"proposal-{index}.json"
        result = tmp_path / f"result-{index}.json"
        _write_incident(incident, incident_id=str(200 + index))
        _write_proposal(proposal, incident_id=str(200 + index))
        results.append(
            module.run_repair(
                incident_path=incident,
                proposal_path=proposal,
                state_path=state,
                result_path=result,
                events_path=events,
                dc_root=tmp_path,
                executor=executor,
                verifier=lambda _incident: (False, "still_down"),
                now=now,
            )
        )

    assert [item["status"] for item in results] == ["failed", "failed", "denied"]
    assert results[2]["reason"] == "circuit_open"
    assert results[1]["circuit_open_until"] == 3_100 + 21_600
    assert len(calls) == 2


def test_persistent_action_runs_fixed_rollback_after_failed_verification() -> None:
    module = _load_module(REPAIR_ENGINE, "watchdog_repair_rollback")
    action = module.RepairActionPolicy(
        action_id="test_persistent_action",
        risk="low",
        command=("fixed-apply",),
        rollback_command=("fixed-rollback",),
        timeout_seconds=10,
        persistent_mutation=True,
    )
    calls: list[tuple[str, ...]] = []

    def executor(command: tuple[str, ...], **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    execution = module.execute_action(
        action,
        {"probe": "tcp:1"},
        executor=executor,
        verifier=lambda _incident: (False, "still_down"),
        env={},
    )

    assert execution["ok"] is False
    assert execution["rollback"] == "succeeded"
    assert calls == [("fixed-apply",), ("fixed-rollback",)]
