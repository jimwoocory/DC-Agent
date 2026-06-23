"""Business engine for Feishu `/god` mode.

The engine is host-agnostic on purpose. AstrBot/Feishu plugins provide
transport, identity and cards; this module owns parsing, planning, approval
idempotency, execution state and audit persistence.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from inspect import isawaitable
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

DC_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_AUDIT_DB_PATH = DC_ROOT / "data" / "god_mode_audit.db"
CARD_SOURCE = "god_mode_approval"

DEFAULT_ALLOWED_CAPABILITIES = (
    "route",
    "memory",
    "harness",
    "hermes",
    "feishu_card",
    "status",
)
DEFAULT_DENY_CAPABILITIES = ("shell", "filesystem_write")

GodRunStatus = Literal[
    "planned",
    "waiting_approval",
    "running",
    "completed",
    "failed",
    "cancelled",
]
GodActionStatus = Literal[
    "pending",
    "approved",
    "executed",
    "rejected",
    "cancelled",
    "blocked",
    "failed",
]
GodCommandKind = Literal["plan", "status", "cancel", "help"]
GodDecision = Literal["approve", "reject"]
GodModeExecutor = Callable[["GodModeAction"], str | dict[str, Any]]
GodModeAsyncExecutor = Callable[
    ["GodModeAction"],
    str | dict[str, Any] | Awaitable[str | dict[str, Any]],
]

_SENSITIVE_KEY_RE = re.compile(
    r"(token|secret|password|api[_-]?key|authorization|credential|private[_-]?key)",
    re.IGNORECASE,
)
_SENSITIVE_VALUE_RE = re.compile(
    r"(?i)\b(token|secret|password|api[_-]?key|authorization|credential)"
    r"\s*[:=]\s*([^\s,;]+)"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_OPENAI_STYLE_SECRET_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")


@dataclass(frozen=True, slots=True)
class GodModeConfig:
    enabled: bool = True
    owners: tuple[str, ...] = ()
    require_approval: bool = True
    allowed_capabilities: tuple[str, ...] = DEFAULT_ALLOWED_CAPABILITIES
    deny_capabilities: tuple[str, ...] = DEFAULT_DENY_CAPABILITIES
    audit_db_path: Path = DEFAULT_AUDIT_DB_PATH

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> GodModeConfig:
        raw = raw or {}
        owners = _string_tuple(raw.get("owners"))
        allowed = _string_tuple(raw.get("allowed_capabilities"))
        denied = _string_tuple(raw.get("deny_capabilities"))
        db_path = raw.get("audit_db_path") or raw.get("db_path")
        return cls(
            enabled=bool(raw.get("enabled", True)),
            owners=owners,
            require_approval=bool(raw.get("require_approval", True)),
            allowed_capabilities=allowed or DEFAULT_ALLOWED_CAPABILITIES,
            deny_capabilities=denied or DEFAULT_DENY_CAPABILITIES,
            audit_db_path=Path(db_path) if db_path else DEFAULT_AUDIT_DB_PATH,
        )


@dataclass(frozen=True, slots=True)
class GodModeRequest:
    text: str
    actor: str
    platform_id: str = ""
    session_id: str = ""
    chat_id: str = ""
    open_id: str = ""
    message_id: str = ""
    sender_id: str = ""
    feishu_ingress_audit_id: str = ""
    actor_is_admin: bool = False


@dataclass(frozen=True, slots=True)
class GodModeCommand:
    kind: GodCommandKind
    text: str = ""
    run_id: str = ""


@dataclass(frozen=True, slots=True)
class GodModeAction:
    action_id: str
    tool_name: str
    capability: str
    description: str
    side_effect: bool
    status: GodActionStatus = "pending"
    input: dict[str, Any] = field(default_factory=dict)
    result_summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "tool_name": self.tool_name,
            "capability": self.capability,
            "description": self.description,
            "side_effect": self.side_effect,
            "status": self.status,
            "input": redact_sensitive(self.input),
            "result_summary": redact_sensitive(self.result_summary),
        }


@dataclass(frozen=True, slots=True)
class GodModeRun:
    run_id: str
    status: GodRunStatus
    summary: str
    actions: tuple[GodModeAction, ...]
    approval_required: bool
    audit_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "summary": redact_sensitive(self.summary),
            "actions": [action.to_dict() for action in self.actions],
            "approval_required": self.approval_required,
            "audit_id": self.audit_id,
        }


def parse_god_command(text: str) -> GodModeCommand:
    body = (text or "").strip()
    if body.startswith("/god"):
        body = body[len("/god") :].strip()
    elif body.casefold().startswith("god"):
        body = body[3:].strip()

    if not body:
        return GodModeCommand(kind="help")

    parts = body.split(maxsplit=1)
    subcommand = parts[0].casefold()
    rest = parts[1].strip() if len(parts) > 1 else ""
    if subcommand == "status":
        return GodModeCommand(kind="status", run_id=rest)
    if subcommand == "cancel":
        return GodModeCommand(kind="cancel", run_id=rest)
    return GodModeCommand(kind="plan", text=body)


def build_god_mode_approval_card(run: GodModeRun) -> dict[str, Any]:
    action = next((item for item in run.actions if item.side_effect), None)
    if action is None and run.actions:
        action = run.actions[0]
    action_id = action.action_id if action else ""
    description = action.description if action else run.summary
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text", "content": "God Mode 审批"},
        },
        "elements": [
            {
                "tag": "markdown",
                "content": (
                    f"**run_id**: `{run.run_id}`\n"
                    f"**action_id**: `{action_id}`\n"
                    f"**状态**: {run.status}\n"
                    f"**动作**: {description}"
                ),
            },
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "批准"},
                        "type": "primary",
                        "value": {
                            "source": CARD_SOURCE,
                            "run_id": run.run_id,
                            "action_id": action_id,
                            "decision": "approve",
                        },
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "拒绝"},
                        "type": "danger",
                        "value": {
                            "source": CARD_SOURCE,
                            "run_id": run.run_id,
                            "action_id": action_id,
                            "decision": "reject",
                        },
                    },
                ],
            },
        ],
    }


def build_god_mode_execution_card(action: GodModeAction) -> dict[str, Any]:
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "green",
            "title": {"tag": "plain_text", "content": "God Mode 已批准执行"},
        },
        "elements": [
            {
                "tag": "markdown",
                "content": (
                    f"**action_id**: `{action.action_id}`\n"
                    f"**工具**: `{action.tool_name}`\n"
                    f"**能力**: {action.capability}\n"
                    f"**说明**: {action.description}"
                ),
            }
        ],
    }


def redact_sensitive(value: Any) -> Any:
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if _SENSITIVE_KEY_RE.search(key_text):
                redacted[key_text] = "<redacted>"
            else:
                redacted[key_text] = redact_sensitive(item)
        return redacted
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive(item) for item in value)
    if isinstance(value, str):
        return _redact_string(value)
    return value


class GodModeEngine:
    def __init__(self, audit_db_path: str | Path = DEFAULT_AUDIT_DB_PATH) -> None:
        self.audit_db_path = Path(audit_db_path)
        self.audit_db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def can_initiate(self, request: GodModeRequest, config: GodModeConfig) -> bool:
        if request.actor_is_admin:
            return True
        actor_ids = {request.actor, request.sender_id, request.open_id}
        actor_ids = {item for item in actor_ids if item}
        return bool(actor_ids.intersection(config.owners))

    def plan(
        self,
        request: GodModeRequest,
        config: GodModeConfig | None = None,
        *,
        executors: Mapping[str, GodModeExecutor] | None = None,
    ) -> GodModeRun:
        config = config or GodModeConfig(audit_db_path=self.audit_db_path)
        command = parse_god_command(request.text)
        if command.kind == "help":
            return self._create_run(
                request,
                status="completed",
                summary=(
                    "用法：/god <任务>、/god status [run_id]、/god cancel <run_id>。"
                ),
                actions=(),
                approval_required=False,
            )
        if command.kind != "plan":
            return self._create_run(
                request,
                status="failed",
                summary="该命令需要由插件入口处理。",
                actions=(),
                approval_required=False,
            )
        if not config.enabled:
            return self._create_run(
                request,
                status="failed",
                summary="God Mode 当前未启用。",
                actions=(),
                approval_required=False,
            )
        if not self.can_initiate(request, config):
            return self._create_run(
                request,
                status="failed",
                summary="权限不足：只有 God Mode owners 或管理员可以发起 /god。",
                actions=(),
                approval_required=False,
                audit_event="request_denied",
            )

        actions = [
            self._with_request_context(action, request)
            for action in self._plan_actions(command.text, config)
        ]
        if not actions:
            return self._create_run(
                request,
                status="completed",
                summary="没有识别到需要执行的动作。",
                actions=(),
                approval_required=False,
            )

        if any(action.status == "blocked" for action in actions):
            return self._create_run(
                request,
                status="failed",
                summary="请求包含被禁用或未允许的能力，已拒绝执行。",
                actions=tuple(actions),
                approval_required=False,
            )

        approval_required = bool(
            config.require_approval and any(action.side_effect for action in actions)
        )
        status: GodRunStatus = "waiting_approval" if approval_required else "planned"
        summary = (
            "已生成执行计划，副作用动作需要管理员审批。"
            if approval_required
            else "只读计划已生成并执行。"
        )
        run = self._create_run(
            request,
            status=status,
            summary=summary,
            actions=tuple(actions),
            approval_required=approval_required,
        )
        if approval_required:
            return run
        return self._execute_read_only_run(run.run_id, executors=executors)

    def get_run(self, run_id: str) -> GodModeRun | None:
        run_id = run_id.strip()
        if not run_id:
            return None
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT run_id, status, summary, approval_required, audit_id
                FROM god_mode_runs
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            if row is None:
                return None
            actions = self._load_actions(conn, run_id)
        return GodModeRun(
            run_id=str(row["run_id"]),
            status=str(row["status"]),  # type: ignore[arg-type]
            summary=str(row["summary"] or ""),
            actions=actions,
            approval_required=bool(row["approval_required"]),
            audit_id=str(row["audit_id"] or ""),
        )

    def latest_run(self) -> GodModeRun | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT run_id
                FROM god_mode_runs
                ORDER BY created_at DESC, rowid DESC
                LIMIT 1
                """
            ).fetchone()
        if row is None:
            return None
        return self.get_run(str(row["run_id"]))

    def cancel_run(self, run_id: str, *, actor: str = "") -> GodModeRun:
        run = self.get_run(run_id)
        if run is None:
            raise LookupError(f"God run not found: {run_id}")
        if run.status in {"completed", "failed", "cancelled"}:
            self._append_audit(
                event="run_cancel_duplicate",
                run_id=run.run_id,
                actor=actor,
                payload={"status": run.status},
            )
            return run

        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE god_mode_runs
                SET status = ?, updated_at = ?
                WHERE run_id = ?
                """,
                ("cancelled", now, run.run_id),
            )
            conn.execute(
                """
                UPDATE god_mode_actions
                SET status = ?
                WHERE run_id = ? AND status IN ('pending', 'approved')
                """,
                ("cancelled", run.run_id),
            )
            conn.commit()
        self._append_audit(
            event="run_cancelled",
            run_id=run.run_id,
            actor=actor,
            payload={"run_id": run.run_id},
        )
        refreshed = self.get_run(run.run_id)
        if refreshed is None:
            raise LookupError(f"God run not found after cancel: {run.run_id}")
        return refreshed

    def apply_decision(
        self,
        run_id: str,
        action_id: str,
        decision: str,
        *,
        actor: str = "",
        executors: Mapping[str, GodModeExecutor] | None = None,
    ) -> GodModeRun:
        normalized = decision.strip().casefold()
        if normalized not in {"approve", "reject"}:
            raise ValueError("decision must be approve or reject")
        decision_typed: GodDecision = normalized  # type: ignore[assignment]

        run = self.get_run(run_id)
        if run is None:
            raise LookupError(f"God run not found: {run_id}")
        action = self._get_action(run_id, action_id)
        if action is None:
            raise LookupError(f"God action not found: {run_id}/{action_id}")

        if action.status in {"executed", "rejected", "cancelled", "blocked", "failed"}:
            self._append_audit(
                event="approval_duplicate",
                run_id=run_id,
                action_id=action_id,
                actor=actor,
                payload={"decision": decision_typed, "status": action.status},
            )
            refreshed = self.get_run(run_id)
            if refreshed is None:
                raise LookupError(f"God run not found after duplicate: {run_id}")
            return refreshed

        if decision_typed == "reject":
            return self._reject_action(run_id, action_id, actor=actor)
        return self._approve_and_execute_action(
            run_id,
            action,
            actor=actor,
            executors=executors,
        )

    async def apply_decision_async(
        self,
        run_id: str,
        action_id: str,
        decision: str,
        *,
        actor: str = "",
        executors: Mapping[str, GodModeAsyncExecutor] | None = None,
    ) -> GodModeRun:
        normalized = decision.strip().casefold()
        if normalized not in {"approve", "reject"}:
            raise ValueError("decision must be approve or reject")
        decision_typed: GodDecision = normalized  # type: ignore[assignment]

        run = self.get_run(run_id)
        if run is None:
            raise LookupError(f"God run not found: {run_id}")
        action = self._get_action(run_id, action_id)
        if action is None:
            raise LookupError(f"God action not found: {run_id}/{action_id}")

        if action.status in {"executed", "rejected", "cancelled", "blocked", "failed"}:
            self._append_audit(
                event="approval_duplicate",
                run_id=run_id,
                action_id=action_id,
                actor=actor,
                payload={"decision": decision_typed, "status": action.status},
            )
            refreshed = self.get_run(run_id)
            if refreshed is None:
                raise LookupError(f"God run not found after duplicate: {run_id}")
            return refreshed

        if decision_typed == "reject":
            return self._reject_action(run_id, action_id, actor=actor)
        return await self._approve_and_execute_action_async(
            run_id,
            action,
            actor=actor,
            executors=executors,
        )

    def audit_events(self, run_id: str) -> tuple[dict[str, Any], ...]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT audit_id, created_at, run_id, action_id, event, actor, payload_json
                FROM god_mode_audit
                WHERE run_id = ?
                ORDER BY created_at ASC, rowid ASC
                """,
                (run_id,),
            ).fetchall()
        events = []
        for row in rows:
            try:
                payload = json.loads(str(row["payload_json"] or "{}"))
            except json.JSONDecodeError:
                payload = {}
            events.append(
                {
                    "audit_id": row["audit_id"],
                    "created_at": row["created_at"],
                    "run_id": row["run_id"],
                    "action_id": row["action_id"],
                    "event": row["event"],
                    "actor": row["actor"],
                    "payload": payload,
                }
            )
        return tuple(events)

    def _plan_actions(
        self,
        task_text: str,
        config: GodModeConfig,
    ) -> list[GodModeAction]:
        text = task_text.strip()
        lowered = text.casefold()
        specs: list[tuple[str, str, str, bool, str]] = []

        if _contains_any(
            lowered,
            (
                "shell",
                "bash",
                "ssh",
                "devops",
                "部署",
                "重启",
                "删除文件",
                "写文件",
                "改配置",
                "filesystem",
            ),
        ):
            specs.append(
                (
                    "dc_agent_run_contract_check",
                    "shell",
                    "Shell/DevOps 类动作被识别为高风险能力。",
                    True,
                    "high_risk_shell_or_devops",
                )
            )
        elif _contains_any(lowered, ("发送", "通知", "飞书卡片", "卡片", "card")):
            specs.append(
                (
                    "dc_agent_send_feishu_card",
                    "feishu_card",
                    "发送或更新飞书卡片。",
                    True,
                    "send_feishu_card",
                )
            )
        elif _contains_any(
            lowered,
            (
                "启动",
                "创建任务",
                "发起",
                "派发",
                "工作流",
                "workflow",
                "hermes",
            ),
        ):
            specs.append(
                (
                    "dc_agent_start_workflow",
                    "harness",
                    "启动 Hermes/Harness 工作流。",
                    True,
                    "start_workflow",
                )
            )
        elif _contains_any(
            lowered, ("写记忆", "保存记忆", "更新记忆", "写规则", "保存规则")
        ):
            specs.append(
                (
                    "dc_agent_query_memory",
                    "memory",
                    "记忆或规则写入需要审批。",
                    True,
                    "write_memory_or_rule",
                )
            )
        elif _contains_any(lowered, ("记忆", "memory", "知识", "检索")):
            specs.append(
                (
                    "dc_agent_query_memory",
                    "memory",
                    "查询 DC-Agent 记忆/知识。",
                    False,
                    "query_memory",
                )
            )
        elif _contains_any(lowered, ("harness", "状态", "status", "进度", "任务")):
            specs.append(
                (
                    "dc_agent_check_task_status",
                    "status",
                    "查询 Harness/DC-Agent 任务状态。",
                    False,
                    "check_task_status",
                )
            )
        elif _contains_any(lowered, ("contract", "合约", "回归", "检查", "验收")):
            specs.append(
                (
                    "dc_agent_run_contract_check",
                    "harness",
                    "查询 Harness contract/check 状态。",
                    False,
                    "contract_check_status",
                )
            )
        else:
            specs.append(
                (
                    "dc_agent_route_message",
                    "route",
                    "路由并判断 DC-Agent 意图。",
                    False,
                    "route_message",
                )
            )

        actions: list[GodModeAction] = []
        for index, (
            tool_name,
            capability,
            description,
            side_effect,
            intent,
        ) in enumerate(
            specs,
            start=1,
        ):
            blocked = capability in set(config.deny_capabilities)
            if capability not in set(config.allowed_capabilities) and not blocked:
                blocked = True
            actions.append(
                GodModeAction(
                    action_id=f"action_{index}",
                    tool_name=tool_name,
                    capability=capability,
                    description=description,
                    side_effect=side_effect,
                    status="blocked" if blocked else "pending",
                    input={
                        "task": text,
                        "intent": intent,
                        "capability": capability,
                    },
                    result_summary=(
                        f"能力 {capability} 已被禁用或未配置允许。" if blocked else ""
                    ),
                )
            )
        return actions

    def _with_request_context(
        self,
        action: GodModeAction,
        request: GodModeRequest,
    ) -> GodModeAction:
        return replace(
            action,
            input={
                **action.input,
                "platform_id": request.platform_id,
                "session_id": request.session_id,
                "chat_id": request.chat_id,
                "open_id": request.open_id,
                "message_id": request.message_id,
                "sender_id": request.sender_id,
                "feishu_ingress_audit_id": request.feishu_ingress_audit_id,
            },
        )

    def _create_run(
        self,
        request: GodModeRequest,
        *,
        status: GodRunStatus,
        summary: str,
        actions: tuple[GodModeAction, ...],
        approval_required: bool,
        audit_event: str = "run_planned",
    ) -> GodModeRun:
        run_id = f"god_{uuid4().hex[:12]}"
        audit_id = self._append_audit(
            event=audit_event,
            run_id=run_id,
            actor=request.actor,
            platform_id=request.platform_id,
            payload={
                "request": _request_payload(request),
                "summary": summary,
                "status": status,
                "approval_required": approval_required,
                "actions": [action.to_dict() for action in actions],
            },
        )
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO god_mode_runs (
                    run_id, created_at, updated_at, status, actor, platform_id,
                    session_id, chat_id, open_id, message_id,
                    feishu_ingress_audit_id, summary, approval_required,
                    request_text, audit_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    now,
                    now,
                    status,
                    request.actor,
                    request.platform_id,
                    request.session_id,
                    request.chat_id,
                    request.open_id,
                    request.message_id,
                    request.feishu_ingress_audit_id,
                    _redact_string(summary),
                    int(approval_required),
                    _redact_string(request.text),
                    audit_id,
                ),
            )
            for action in actions:
                self._insert_action(conn, run_id, action)
            conn.commit()
        return GodModeRun(
            run_id=run_id,
            status=status,
            summary=_redact_string(summary),
            actions=actions,
            approval_required=approval_required,
            audit_id=audit_id,
        )

    def _execute_read_only_run(
        self,
        run_id: str,
        *,
        executors: Mapping[str, GodModeExecutor] | None,
    ) -> GodModeRun:
        run = self.get_run(run_id)
        if run is None:
            raise LookupError(f"God run not found: {run_id}")
        for action in run.actions:
            if action.side_effect or action.status != "pending":
                continue
            self._execute_action(run_id, action, actor="", executors=executors)
        self._refresh_run_status(run_id)
        refreshed = self.get_run(run_id)
        if refreshed is None:
            raise LookupError(f"God run not found after execution: {run_id}")
        return refreshed

    def _approve_and_execute_action(
        self,
        run_id: str,
        action: GodModeAction,
        *,
        actor: str,
        executors: Mapping[str, GodModeExecutor] | None,
    ) -> GodModeRun:
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE god_mode_runs
                SET status = ?, updated_at = ?
                WHERE run_id = ?
                """,
                ("running", now, run_id),
            )
            conn.execute(
                """
                UPDATE god_mode_actions
                SET status = ?
                WHERE run_id = ? AND action_id = ? AND status = 'pending'
                """,
                ("approved", run_id, action.action_id),
            )
            conn.commit()
        self._append_audit(
            event="approval_approved",
            run_id=run_id,
            action_id=action.action_id,
            actor=actor,
            payload={"decision": "approve"},
        )
        self._execute_action(run_id, action, actor=actor, executors=executors)
        self._refresh_run_status(run_id)
        refreshed = self.get_run(run_id)
        if refreshed is None:
            raise LookupError(f"God run not found after approval: {run_id}")
        return refreshed

    async def _approve_and_execute_action_async(
        self,
        run_id: str,
        action: GodModeAction,
        *,
        actor: str,
        executors: Mapping[str, GodModeAsyncExecutor] | None,
    ) -> GodModeRun:
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE god_mode_runs
                SET status = ?, updated_at = ?
                WHERE run_id = ?
                """,
                ("running", now, run_id),
            )
            conn.execute(
                """
                UPDATE god_mode_actions
                SET status = ?
                WHERE run_id = ? AND action_id = ? AND status = 'pending'
                """,
                ("approved", run_id, action.action_id),
            )
            conn.commit()
        self._append_audit(
            event="approval_approved",
            run_id=run_id,
            action_id=action.action_id,
            actor=actor,
            payload={"decision": "approve"},
        )
        await self._execute_action_async(
            run_id,
            action,
            actor=actor,
            executors=executors,
        )
        self._refresh_run_status(run_id)
        refreshed = self.get_run(run_id)
        if refreshed is None:
            raise LookupError(f"God run not found after approval: {run_id}")
        return refreshed

    def _reject_action(self, run_id: str, action_id: str, *, actor: str) -> GodModeRun:
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE god_mode_actions
                SET status = ?
                WHERE run_id = ? AND action_id = ? AND status IN ('pending', 'approved')
                """,
                ("rejected", run_id, action_id),
            )
            conn.execute(
                """
                UPDATE god_mode_runs
                SET updated_at = ?
                WHERE run_id = ?
                """,
                (now, run_id),
            )
            conn.commit()
        self._append_audit(
            event="approval_rejected",
            run_id=run_id,
            action_id=action_id,
            actor=actor,
            payload={"decision": "reject"},
        )
        self._refresh_run_status(run_id)
        refreshed = self.get_run(run_id)
        if refreshed is None:
            raise LookupError(f"God run not found after rejection: {run_id}")
        return refreshed

    def _execute_action(
        self,
        run_id: str,
        action: GodModeAction,
        *,
        actor: str,
        executors: Mapping[str, GodModeExecutor] | None,
    ) -> None:
        stored = self._get_action(run_id, action.action_id)
        if stored is None:
            raise LookupError(f"God action not found: {run_id}/{action.action_id}")
        if stored.status == "executed":
            self._append_audit(
                event="action_execute_duplicate",
                run_id=run_id,
                action_id=action.action_id,
                actor=actor,
                payload={"status": stored.status},
            )
            return

        try:
            result_summary = self._call_executor(stored, executors)
            status: GodActionStatus = "executed"
            event = "action_executed"
        except Exception as exc:  # noqa: BLE001
            result_summary = f"{type(exc).__name__}: {exc}"
            status = "failed"
            event = "action_failed"

        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE god_mode_actions
                SET status = ?, result_summary = ?, executed_at = ?
                WHERE run_id = ? AND action_id = ?
                """,
                (
                    status,
                    _redact_string(result_summary),
                    now if status == "executed" else "",
                    run_id,
                    action.action_id,
                ),
            )
            conn.execute(
                "UPDATE god_mode_runs SET updated_at = ? WHERE run_id = ?",
                (now, run_id),
            )
            conn.commit()
        self._append_audit(
            event=event,
            run_id=run_id,
            action_id=action.action_id,
            actor=actor,
            payload={"result_summary": result_summary, "tool": action.tool_name},
        )

    async def _execute_action_async(
        self,
        run_id: str,
        action: GodModeAction,
        *,
        actor: str,
        executors: Mapping[str, GodModeAsyncExecutor] | None,
    ) -> None:
        stored = self._get_action(run_id, action.action_id)
        if stored is None:
            raise LookupError(f"God action not found: {run_id}/{action.action_id}")
        if stored.status == "executed":
            self._append_audit(
                event="action_execute_duplicate",
                run_id=run_id,
                action_id=action.action_id,
                actor=actor,
                payload={"status": stored.status},
            )
            return

        try:
            result_summary = await self._call_executor_async(stored, executors)
            status: GodActionStatus = "executed"
            event = "action_executed"
        except Exception as exc:  # noqa: BLE001
            result_summary = f"{type(exc).__name__}: {exc}"
            status = "failed"
            event = "action_failed"

        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE god_mode_actions
                SET status = ?, result_summary = ?, executed_at = ?
                WHERE run_id = ? AND action_id = ?
                """,
                (
                    status,
                    _redact_string(result_summary),
                    now if status == "executed" else "",
                    run_id,
                    action.action_id,
                ),
            )
            conn.execute(
                "UPDATE god_mode_runs SET updated_at = ? WHERE run_id = ?",
                (now, run_id),
            )
            conn.commit()
        self._append_audit(
            event=event,
            run_id=run_id,
            action_id=action.action_id,
            actor=actor,
            payload={"result_summary": result_summary, "tool": action.tool_name},
        )

    def _call_executor(
        self,
        action: GodModeAction,
        executors: Mapping[str, GodModeExecutor] | None,
    ) -> str:
        if executors and action.tool_name in executors:
            result = executors[action.tool_name](action)
            safe = redact_sensitive(result)
            if isinstance(safe, str):
                return safe
            return json.dumps(safe, ensure_ascii=False, sort_keys=True)
        if action.side_effect:
            return f"{action.tool_name} 已审批并记录；当前未配置真实执行器。"
        return _default_read_only_result(action)

    async def _call_executor_async(
        self,
        action: GodModeAction,
        executors: Mapping[str, GodModeAsyncExecutor] | None,
    ) -> str:
        if executors and action.tool_name in executors:
            result = executors[action.tool_name](action)
            if isawaitable(result):
                result = await result
            safe = redact_sensitive(result)
            if isinstance(safe, str):
                return safe
            return json.dumps(safe, ensure_ascii=False, sort_keys=True)
        if action.side_effect:
            return f"{action.tool_name} 已审批并记录；当前未配置真实执行器。"
        return _default_read_only_result(action)

    def _refresh_run_status(self, run_id: str) -> None:
        run = self.get_run(run_id)
        if run is None:
            return
        statuses = {action.status for action in run.actions}
        if any(status == "failed" for status in statuses):
            new_status: GodRunStatus = "failed"
        elif statuses and all(status == "executed" for status in statuses):
            new_status = "completed"
        elif statuses and all(
            status in {"rejected", "cancelled"} for status in statuses
        ):
            new_status = "cancelled"
        elif any(status in {"pending", "approved"} for status in statuses):
            new_status = "waiting_approval" if run.approval_required else "running"
        else:
            new_status = run.status
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE god_mode_runs
                SET status = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (new_status, now, run_id),
            )
            conn.commit()

    def _append_audit(
        self,
        *,
        event: str,
        run_id: str,
        action_id: str = "",
        actor: str = "",
        platform_id: str = "",
        payload: Mapping[str, Any] | None = None,
    ) -> str:
        audit_id = f"audit_{uuid4().hex[:12]}"
        safe_payload = redact_sensitive(dict(payload or {}))
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO god_mode_audit (
                    audit_id, created_at, run_id, action_id, event, actor,
                    platform_id, payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    audit_id,
                    _utc_now(),
                    run_id,
                    action_id,
                    event,
                    actor,
                    platform_id,
                    json.dumps(safe_payload, ensure_ascii=False, sort_keys=True),
                ),
            )
            conn.commit()
        return audit_id

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS god_mode_runs (
                    run_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    platform_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    open_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    feishu_ingress_audit_id TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    approval_required INTEGER NOT NULL,
                    request_text TEXT NOT NULL,
                    audit_id TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS god_mode_actions (
                    run_id TEXT NOT NULL,
                    action_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    capability TEXT NOT NULL,
                    description TEXT NOT NULL,
                    side_effect INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    input_json TEXT NOT NULL,
                    result_summary TEXT NOT NULL DEFAULT '',
                    executed_at TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (run_id, action_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS god_mode_audit (
                    audit_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    action_id TEXT NOT NULL,
                    event TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    platform_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.audit_db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _insert_action(
        self,
        conn: sqlite3.Connection,
        run_id: str,
        action: GodModeAction,
    ) -> None:
        conn.execute(
            """
            INSERT INTO god_mode_actions (
                run_id, action_id, tool_name, capability, description,
                side_effect, status, input_json, result_summary
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                action.action_id,
                action.tool_name,
                action.capability,
                action.description,
                int(action.side_effect),
                action.status,
                json.dumps(redact_sensitive(action.input), ensure_ascii=False),
                _redact_string(action.result_summary),
            ),
        )

    def _load_actions(
        self,
        conn: sqlite3.Connection,
        run_id: str,
    ) -> tuple[GodModeAction, ...]:
        rows = conn.execute(
            """
            SELECT action_id, tool_name, capability, description, side_effect,
                   status, input_json, result_summary
            FROM god_mode_actions
            WHERE run_id = ?
            ORDER BY action_id ASC
            """,
            (run_id,),
        ).fetchall()
        actions = []
        for row in rows:
            try:
                input_payload = json.loads(str(row["input_json"] or "{}"))
            except json.JSONDecodeError:
                input_payload = {}
            actions.append(
                GodModeAction(
                    action_id=str(row["action_id"]),
                    tool_name=str(row["tool_name"]),
                    capability=str(row["capability"]),
                    description=str(row["description"]),
                    side_effect=bool(row["side_effect"]),
                    status=str(row["status"]),  # type: ignore[arg-type]
                    input=input_payload if isinstance(input_payload, dict) else {},
                    result_summary=str(row["result_summary"] or ""),
                )
            )
        return tuple(actions)

    def _get_action(self, run_id: str, action_id: str) -> GodModeAction | None:
        with self._connect() as conn:
            actions = self._load_actions(conn, run_id)
        for action in actions:
            if action.action_id == action_id:
                return action
        return None


def _request_payload(request: GodModeRequest) -> dict[str, Any]:
    return {
        "text": request.text,
        "actor": request.actor,
        "platform_id": request.platform_id,
        "session_id": request.session_id,
        "chat_id": request.chat_id,
        "open_id": request.open_id,
        "message_id": request.message_id,
        "sender_id": request.sender_id,
        "feishu_ingress_audit_id": request.feishu_ingress_audit_id,
        "actor_is_admin": request.actor_is_admin,
    }


def _string_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if not isinstance(value, list | tuple | set):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _contains_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(needle.casefold() in text for needle in needles)


def _default_read_only_result(action: GodModeAction) -> str:
    if action.tool_name == "dc_agent_check_task_status":
        return "Harness/DC-Agent 状态查询完成：当前 /god run 无未完成审批动作。"
    if action.tool_name == "dc_agent_query_memory":
        return "记忆/知识查询已完成：首版返回安全摘要，不读取敏感配置。"
    if action.tool_name == "dc_agent_run_contract_check":
        return "Contract/check 状态查询完成：未触发 shell 或外部执行。"
    return "路由/意图判断完成：未触发副作用。"


def _redact_string(value: str) -> str:
    redacted = _SENSITIVE_VALUE_RE.sub(
        lambda match: f"{match.group(1)}=<redacted>", value
    )
    redacted = _BEARER_RE.sub("Bearer <redacted>", redacted)
    return _OPENAI_STYLE_SECRET_RE.sub("sk-<redacted>", redacted)


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
