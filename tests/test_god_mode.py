from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dc_engines"))

from dc_engines.god_mode import (  # noqa: E402
    GodModeConfig,
    GodModeEngine,
    GodModeRequest,
    parse_god_command,
)


def _engine(tmp_path: Path) -> GodModeEngine:
    return GodModeEngine(tmp_path / "god_mode.db")


def _config(tmp_path: Path) -> GodModeConfig:
    return GodModeConfig.from_dict(
        {
            "owners": ["ou_admin"],
            "audit_db_path": str(tmp_path / "god_mode.db"),
        }
    )


def _request(
    text: str, *, actor: str = "ou_admin", admin: bool = True
) -> GodModeRequest:
    return GodModeRequest(
        text=text,
        actor=actor,
        platform_id="lark",
        session_id="lark:private:ou_admin",
        chat_id="oc_private",
        open_id=actor,
        message_id="om_message",
        sender_id=actor,
        feishu_ingress_audit_id="audit_feishu_1",
        actor_is_admin=admin,
    )


def test_parse_god_command_variants() -> None:
    assert parse_god_command("/god 查询当前 Harness 状态").kind == "plan"

    status = parse_god_command("/god status god_123")
    assert status.kind == "status"
    assert status.run_id == "god_123"

    cancel = parse_god_command("god cancel god_456")
    assert cancel.kind == "cancel"
    assert cancel.run_id == "god_456"

    assert parse_god_command("/god").kind == "help"


def test_non_owner_request_is_refused(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    config = _config(tmp_path)

    run = engine.plan(
        _request("/god 发送飞书卡片通知", actor="ou_user", admin=False),
        config,
    )

    assert run.status == "failed"
    assert "权限不足" in run.summary
    assert run.actions == ()


def test_read_only_harness_status_completes_without_approval(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    config = _config(tmp_path)

    run = engine.plan(_request("/god 查询当前 Harness 状态"), config)

    assert run.status == "completed"
    assert run.approval_required is False
    assert run.actions[0].tool_name == "dc_agent_check_task_status"
    assert run.actions[0].status == "executed"


def test_side_effect_action_requires_approval(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    config = _config(tmp_path)

    run = engine.plan(_request("/god 发送飞书卡片通知项目群"), config)

    assert run.status == "waiting_approval"
    assert run.approval_required is True
    assert run.actions[0].tool_name == "dc_agent_send_feishu_card"
    assert run.actions[0].side_effect is True
    assert run.actions[0].status == "pending"


def test_approve_action_is_idempotent(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    config = _config(tmp_path)
    calls: list[str] = []
    run = engine.plan(_request("/god 发送飞书卡片通知项目群"), config)

    def executor(action):
        calls.append(action.action_id)
        return "sent once"

    approved = engine.apply_decision(
        run.run_id,
        "action_1",
        "approve",
        actor="ou_admin",
        executors={"dc_agent_send_feishu_card": executor},
    )
    duplicate = engine.apply_decision(
        run.run_id,
        "action_1",
        "approve",
        actor="ou_admin",
        executors={"dc_agent_send_feishu_card": executor},
    )

    assert approved.status == "completed"
    assert duplicate.status == "completed"
    assert calls == ["action_1"]
    events = engine.audit_events(run.run_id)
    assert [event["event"] for event in events].count("action_executed") == 1
    assert [event["event"] for event in events].count("approval_duplicate") == 1


def test_reject_action_is_idempotent(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    config = _config(tmp_path)
    run = engine.plan(_request("/god 启动 Hermes 工作流"), config)

    rejected = engine.apply_decision(
        run.run_id,
        "action_1",
        "reject",
        actor="ou_admin",
    )
    duplicate = engine.apply_decision(
        run.run_id,
        "action_1",
        "reject",
        actor="ou_admin",
    )

    assert rejected.status == "cancelled"
    assert duplicate.status == "cancelled"
    assert rejected.actions[0].status == "rejected"
    events = engine.audit_events(run.run_id)
    assert [event["event"] for event in events].count("approval_rejected") == 1


def test_audit_redacts_sensitive_values(tmp_path: Path) -> None:
    db_path = tmp_path / "god_mode.db"
    engine = GodModeEngine(db_path)
    config = GodModeConfig.from_dict(
        {
            "owners": ["ou_admin"],
            "audit_db_path": str(db_path),
        }
    )

    run = engine.plan(
        _request("/god 查询当前 Harness 状态 token=super-secret sk-abcdef123456"),
        config,
    )

    assert run.status == "completed"
    with sqlite3.connect(db_path) as conn:
        raw = "\n".join(
            str(item[0])
            for item in conn.execute(
                "SELECT request_text FROM god_mode_runs UNION ALL "
                "SELECT payload_json FROM god_mode_audit"
            ).fetchall()
        )
    assert "super-secret" not in raw
    assert "sk-abcdef123456" not in raw
    assert "<redacted>" in raw
