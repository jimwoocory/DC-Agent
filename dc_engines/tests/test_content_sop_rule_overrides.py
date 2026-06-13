from __future__ import annotations

import json
from pathlib import Path

from dc_engines.department_workflows import (
    build_content_sop_workflow_payload,
    match_department_workflow,
)
from dc_engines.department_workflows.content_rule_overrides import (
    ContentSopRuleOverrideError,
    apply_rule_proposal_to_overrides,
    load_content_sop_rule_overrides,
    rollback_rule_override,
)


def _proposal(status: str = "approved_for_runtime") -> dict:
    return {
        "proposal_id": "proposal_no_email",
        "department_id": "client_dept",
        "scenario_id": "customer_greeting",
        "rule_type": "process",
        "rule_text": "客户触达默认使用飞书和私域，不默认使用邮件。",
        "support_count": 3,
        "evidence_candidate_ids": ["cand_1", "cand_2", "cand_3"],
        "status": status,
    }


def _client_greeting_payload(path: Path) -> dict:
    match = match_department_workflow(
        employee_department="客户部",
        text="帮我写客户问候话术",
        min_score=1,
    )
    assert match is not None
    return build_content_sop_workflow_payload(
        match,
        source="test",
        message_text="帮我写客户问候话术",
        content_type="copy",
        rule_overrides_path=path,
    )


def test_apply_rule_proposal_requires_runtime_approval(tmp_path: Path) -> None:
    path = tmp_path / "content_sop_rule_overrides.json"

    result = apply_rule_proposal_to_overrides(
        _proposal(status="pending"),
        path=path,
        actor="tester",
        now="2026-06-05T00:00:00Z",
    )

    assert result.applied is False
    assert result.reason == "proposal is not approved for runtime"
    assert not path.exists()


def test_apply_rule_proposal_rejects_generic_memory_approval(tmp_path: Path) -> None:
    path = tmp_path / "content_sop_rule_overrides.json"

    result = apply_rule_proposal_to_overrides(
        _proposal(status="approved"),
        path=path,
        actor="tester",
        now="2026-06-05T00:00:00Z",
    )

    assert result.applied is False
    assert result.reason == "proposal is not approved for runtime"
    assert not path.exists()


def test_apply_rule_proposal_writes_versioned_override(tmp_path: Path) -> None:
    path = tmp_path / "content_sop_rule_overrides.json"

    result = apply_rule_proposal_to_overrides(
        _proposal(),
        path=path,
        actor="tester",
        now="2026-06-05T00:00:00Z",
    )

    data = json.loads(path.read_text(encoding="utf-8"))
    assert result.applied is True
    assert data["version"] == 2
    assert data["rules"][0]["proposal_id"] == "proposal_no_email"
    assert data["rules"][0]["enabled"] is True
    assert data["audit"][0]["action"] == "apply_rule_proposal"


def test_apply_rule_proposal_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "content_sop_rule_overrides.json"

    first = apply_rule_proposal_to_overrides(
        _proposal(),
        path=path,
        actor="tester",
        now="2026-06-05T00:00:00Z",
    )
    second = apply_rule_proposal_to_overrides(
        _proposal(),
        path=path,
        actor="tester",
        now="2026-06-05T00:01:00Z",
    )

    data = load_content_sop_rule_overrides(path)
    assert first.applied is True
    assert second.applied is False
    assert second.reason == "proposal already applied"
    assert data["version"] == 2
    assert len(data["rules"]) == 1


def test_rule_override_save_keeps_rollback_backup(tmp_path: Path) -> None:
    path = tmp_path / "content_sop_rule_overrides.json"
    apply_rule_proposal_to_overrides(
        _proposal("approved_for_runtime"),
        path=path,
        actor="tester",
        now="2026-06-05T00:00:00Z",
    )

    rollback_rule_override(
        "proposal_no_email",
        path=path,
        actor="tester",
        now="2026-06-05T00:02:00Z",
    )

    backups = sorted(
        (path.parent / "content_sop_rule_overrides_backups").glob(
            "content_sop_rule_overrides.json.rollback-from-v*"
        )
    )
    assert backups
    assert json.loads(backups[0].read_text(encoding="utf-8"))["version"] == 2
    assert not list(path.parent.glob("content_sop_rule_overrides.json.rollback-*"))
    assert not list(path.parent.glob("content_sop_rule_overrides.json.tmp"))


def test_rule_override_load_fails_closed_on_malformed_json(tmp_path: Path) -> None:
    path = tmp_path / "content_sop_rule_overrides.json"
    path.write_text("{not-json", encoding="utf-8")

    try:
        load_content_sop_rule_overrides(path)
    except ContentSopRuleOverrideError as exc:
        assert "malformed content SOP rule override config" in str(exc)
    else:
        raise AssertionError("malformed JSON must fail closed")


def test_rule_override_load_fails_closed_on_invalid_schema(tmp_path: Path) -> None:
    path = tmp_path / "content_sop_rule_overrides.json"
    path.write_text(
        json.dumps({"version": "bad", "rules": {}, "audit": []}), encoding="utf-8"
    )

    try:
        load_content_sop_rule_overrides(path)
    except ContentSopRuleOverrideError as exc:
        assert "invalid content SOP rule override version" in str(exc)
    else:
        raise AssertionError("invalid schema must fail closed")


def test_rule_override_load_fails_closed_on_invalid_rule_entry(tmp_path: Path) -> None:
    path = tmp_path / "content_sop_rule_overrides.json"
    path.write_text(
        json.dumps({"version": 2, "rules": [123], "audit": []}),
        encoding="utf-8",
    )

    try:
        load_content_sop_rule_overrides(path)
    except ContentSopRuleOverrideError as exc:
        assert "invalid content SOP rule entry at index 0" in str(exc)
    else:
        raise AssertionError("invalid rule entry must fail closed")


def test_content_sop_payload_hot_reads_matching_rule(tmp_path: Path) -> None:
    path = tmp_path / "content_sop_rule_overrides.json"
    apply_rule_proposal_to_overrides(
        _proposal(),
        path=path,
        actor="tester",
        now="2026-06-05T00:00:00Z",
    )

    payload = _client_greeting_payload(path)

    rules = payload["runtime_sop_rule_overrides"]
    assert rules[0]["proposal_id"] == "proposal_no_email"
    assert (
        "客户触达默认使用飞书和私域，不默认使用邮件。" in payload["truth_requirements"]
    )
    assert payload["spiral_evolution"]["scope"]["department_id"] == "client_dept"


def test_rollback_rule_override_disables_runtime_rule(tmp_path: Path) -> None:
    path = tmp_path / "content_sop_rule_overrides.json"
    apply_rule_proposal_to_overrides(
        _proposal(),
        path=path,
        actor="tester",
        now="2026-06-05T00:00:00Z",
    )

    rollback = rollback_rule_override(
        "proposal_no_email",
        path=path,
        actor="tester",
        now="2026-06-05T00:02:00Z",
    )
    payload = _client_greeting_payload(path)
    data = load_content_sop_rule_overrides(path)

    assert rollback.applied is True
    assert data["version"] == 3
    assert data["rules"][0]["enabled"] is False
    assert "runtime_sop_rule_overrides" not in payload
