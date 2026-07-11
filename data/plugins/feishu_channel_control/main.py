"""OpenClaw-style Feishu/Lark channel governance for DC-Agent.

The plugin integrates at AstrBot's event layer. It does not replace the Lark
adapter and does not call Feishu APIs directly; API calls stay centralized in
``dc_engines.feishu_hub`` and existing Feishu feature plugins.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dc_engines.feishu_channel_control import (
    FeishuChannelConfig,
    FeishuChannelController,
    FeishuChannelState,
    FeishuPeer,
    RouteBinding,
)

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.star import Context, Star, register

PLUGIN_ID = "feishu_channel_control"


def _call_event_getter(event: AstrMessageEvent, name: str) -> str:
    getter = getattr(event, name, None)
    if not callable(getter):
        return ""
    try:
        return str(getter() or "")
    except Exception:  # noqa: BLE001
        return ""


@register(
    PLUGIN_ID,
    "dc_agent",
    "飞书通道治理：准入、配对、群策略、路由元数据和卡片回调防伪",
    "0.1.0",
)
class FeishuChannelControlPlugin(Star):
    def __init__(self, context: Context, config: dict[str, Any] | None = None) -> None:
        super().__init__(context, config)
        self.project_root = Path(__file__).resolve().parents[3]
        self.config = FeishuChannelConfig.from_dict(config)
        state_path = str((config or {}).get("state_path") or "").strip()
        self.state_path = (
            Path(state_path)
            if state_path
            else self.project_root
            / "data"
            / "config"
            / "feishu_channel_control_state.json"
        )
        self.state = FeishuChannelState(self.state_path)
        self.controller = FeishuChannelController(self.config, self.state)

    async def initialize(self) -> None:
        self.context.feishu_channel_control = self
        self._ensure_ingress_audit_schema()
        from astrbot.core.platform.sources.lark.lark_event import (
            set_lark_egress_auditor,
        )

        set_lark_egress_auditor(self._record_egress_audit)
        synced = self._sync_employee_directory_approvals()
        logger.info(
            "[feishu_channel_control] enabled=%s dm=%s group=%s state=%s employee_synced=%s",
            self.config.enabled,
            self.config.dm_policy,
            self.config.group_policy,
            self.state_path,
            synced,
        )

    async def terminate(self) -> None:
        from astrbot.core.platform.sources.lark.lark_event import (
            set_lark_egress_auditor,
        )

        set_lark_egress_auditor(None)
        if getattr(self.context, "feishu_channel_control", None) is self:
            self.context.feishu_channel_control = None

    @filter.command(
        "feishu-control",
        desc=(
            "/feishu-control status|pairing list|pairing approve <code>|"
            "pairing revoke <open_id>|bind user|group <id> <agent_id> [workspace]"
        ),
    )
    async def feishu_control_command(self, event: AstrMessageEvent) -> None:
        text = (event.message_str or "").strip()
        for prefix in ("/feishu-control", "feishu-control"):
            if text.startswith(prefix):
                text = text[len(prefix) :].strip()
                break
        sender = event.get_sender_id() or ""
        if self.config.owners and sender not in self.config.owners:
            self._reply(event, "只有飞书通道管理员可以执行该命令。")
            return
        if not text or text in {"status", "状态"}:
            self._reply(event, self._format_status())
            return
        parts = text.split()
        if parts[:2] == ["pairing", "list"]:
            self._reply(event, self._format_pairings())
            return
        if parts[:2] == ["pairing", "approve"] and len(parts) >= 3:
            open_id = self.state.approve_pairing(parts[2], approver=sender)
            if open_id:
                self._reply(event, f"已批准：{open_id}")
            else:
                self._reply(event, "配对码不存在或已过期。")
            return
        if parts[:2] == ["pairing", "revoke"] and len(parts) >= 3:
            ok = self.state.revoke(parts[2])
            self._reply(event, "已撤销。" if ok else "该 open_id 未批准。")
            return
        if parts and parts[0] == "bind" and len(parts) >= 4:
            kind = parts[1]
            if kind not in {"user", "group"}:
                self._reply(event, "bind 只支持 user 或 group。")
                return
            peer_id = parts[2]
            agent_id = parts[3]
            workspace = parts[4] if len(parts) >= 5 else ""
            key = f"{'direct' if kind == 'user' else 'group'}:{peer_id}"
            self.state.set_binding(
                key,
                RouteBinding(agent_id=agent_id, workspace=workspace),
            )
            self._reply(event, f"已绑定 {key} → {agent_id}")
            return
        self._reply(event, self._usage())

    @filter.event_message_type(
        EventMessageType.GROUP_MESSAGE | EventMessageType.PRIVATE_MESSAGE,
        priority=20,
    )
    async def on_message(self, event: AstrMessageEvent) -> None:
        if not self._is_lark_event(event):
            return
        peer = self._peer_from_event(event)
        decision = self.controller.decide(peer)
        self._record_ingress_audit(event, peer, decision)
        for key, value in decision.metadata().items():
            event.set_extra(key, value)
        if decision.allowed:
            return
        if decision.reply_text:
            self._reply(event, decision.reply_text)
            return
        if decision.stop_event:
            event.stop_event()

    def _peer_from_event(self, event: AstrMessageEvent) -> FeishuPeer:
        sender_id = event.get_sender_id() or ""
        raw_msg = getattr(event, "message_obj", None)
        chat_id = str(getattr(raw_msg, "chat_id", "") or "")
        group_id = event.get_group_id() or ""
        raw_type = str(getattr(raw_msg, "type", "") or "")
        is_group = bool(group_id) or "GROUP" in raw_type.upper()
        peer_id = (group_id or chat_id) if is_group else sender_id
        return FeishuPeer(
            kind="group" if is_group else "direct",
            peer_id=peer_id,
            sender_id=sender_id,
            platform_id=event.get_platform_id() or "",
            message_text=(event.message_str or "").strip(),
            mentioned=bool(getattr(event, "is_at_or_wake_command", False)),
            trusted_card_action=self._is_trusted_card_action(event),
        )

    def _record_ingress_audit(
        self,
        event: AstrMessageEvent,
        peer: FeishuPeer,
        decision: Any,
    ) -> None:
        if not hasattr(self, "project_root"):
            return
        try:
            db_path = self._audit_db_path()
            db_path.parent.mkdir(parents=True, exist_ok=True)
            audit_id = uuid.uuid4().hex
            now = datetime.now(timezone.utc).isoformat()
            raw_msg = getattr(event, "message_obj", None)
            chat_id = str(getattr(raw_msg, "chat_id", "") or "")
            raw_type = str(getattr(raw_msg, "type", "") or "")
            message_id = str(
                getattr(raw_msg, "message_id", "")
                or getattr(raw_msg, "id", "")
                or getattr(raw_msg, "msg_id", "")
                or ""
            )
            sender_name = _call_event_getter(event, "get_sender_name")
            payload = {
                "message_id": message_id,
                "raw_type": raw_type,
                "chat_id": chat_id,
                "group_id": event.get_group_id() or "",
            }
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS feishu_ingress_audit (
                        audit_id TEXT PRIMARY KEY,
                        received_at TEXT NOT NULL,
                        platform_id TEXT NOT NULL,
                        sender_id TEXT NOT NULL,
                        sender_name TEXT NOT NULL,
                        peer_kind TEXT NOT NULL,
                        peer_id TEXT NOT NULL,
                        text TEXT NOT NULL,
                        allowed INTEGER NOT NULL,
                        policy_reason TEXT NOT NULL,
                        agent_id TEXT NOT NULL,
                        workspace TEXT NOT NULL,
                        mentioned INTEGER NOT NULL,
                        trusted_card_action INTEGER NOT NULL,
                        payload_json TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO feishu_ingress_audit (
                        audit_id, received_at, platform_id, sender_id, sender_name,
                        peer_kind, peer_id, text, allowed, policy_reason, agent_id,
                        workspace, mentioned, trusted_card_action, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        audit_id,
                        now,
                        event.get_platform_id() or "",
                        peer.sender_id,
                        sender_name,
                        peer.kind,
                        peer.peer_id,
                        peer.message_text,
                        1 if decision.allowed else 0,
                        decision.reason,
                        decision.agent_id,
                        decision.workspace,
                        1 if peer.mentioned else 0,
                        1 if peer.trusted_card_action else 0,
                        json.dumps(payload, ensure_ascii=False),
                    ),
                )
                conn.commit()
            event.set_extra("feishu_ingress_audit_id", audit_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[feishu_channel_control] 写入入口审计失败：%s", exc)

    def _ensure_ingress_audit_schema(self) -> None:
        """Create the lossless Feishu ingress audit table at plugin startup."""
        try:
            db_path = self._audit_db_path()
            db_path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS feishu_ingress_audit (
                        audit_id TEXT PRIMARY KEY,
                        received_at TEXT NOT NULL,
                        platform_id TEXT NOT NULL,
                        sender_id TEXT NOT NULL,
                        sender_name TEXT NOT NULL,
                        peer_kind TEXT NOT NULL,
                        peer_id TEXT NOT NULL,
                        text TEXT NOT NULL,
                        allowed INTEGER NOT NULL,
                        policy_reason TEXT NOT NULL,
                        agent_id TEXT NOT NULL,
                        workspace TEXT NOT NULL,
                        mentioned INTEGER NOT NULL,
                        trusted_card_action INTEGER NOT NULL,
                        payload_json TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_feishu_ingress_audit_time
                    ON feishu_ingress_audit(received_at DESC)
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_feishu_ingress_audit_sender
                    ON feishu_ingress_audit(sender_id, received_at DESC)
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS feishu_egress_audit (
                        audit_id TEXT PRIMARY KEY,
                        sent_at TEXT NOT NULL,
                        reply_message_id TEXT NOT NULL,
                        receive_id TEXT NOT NULL,
                        receive_id_type TEXT NOT NULL,
                        msg_type TEXT NOT NULL,
                        success INTEGER NOT NULL,
                        response_code TEXT NOT NULL,
                        response_message_id TEXT NOT NULL,
                        content_chars INTEGER NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_feishu_egress_reply
                    ON feishu_egress_audit(reply_message_id, sent_at DESC)
                    """
                )
                conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[feishu_channel_control] 初始化入口审计表失败：%s", exc)

    def _record_egress_audit(self, payload: dict[str, Any]) -> None:
        """Persist one privacy-light Feishu delivery result.

        Args:
            payload: Delivery metadata emitted by the Lark transport.
        """
        try:
            with sqlite3.connect(self._audit_db_path()) as conn:
                conn.execute(
                    """
                    INSERT INTO feishu_egress_audit (
                        audit_id, sent_at, reply_message_id, receive_id,
                        receive_id_type, msg_type, success, response_code,
                        response_message_id, content_chars
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        uuid.uuid4().hex,
                        datetime.now(timezone.utc).isoformat(),
                        str(payload.get("reply_message_id") or ""),
                        str(payload.get("receive_id") or ""),
                        str(payload.get("receive_id_type") or ""),
                        str(payload.get("msg_type") or ""),
                        1 if payload.get("success") else 0,
                        str(payload.get("response_code") or ""),
                        str(payload.get("response_message_id") or ""),
                        int(payload.get("content_chars") or 0),
                    ),
                )
                conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[feishu_channel_control] 写入出口审计失败：%s", exc)

    def _sync_employee_directory_approvals(self) -> int:
        if not self.config.auto_approve_employee_directory:
            return 0
        db_path = self.project_root / "data" / "employees.db"
        if not db_path.exists():
            return 0
        synced = 0
        try:
            with sqlite3.connect(db_path) as conn:
                rows = conn.execute(
                    """
                    SELECT open_id, display_name, department
                    FROM employees
                    WHERE open_id LIKE 'ou_%'
                    """
                ).fetchall()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[feishu_channel_control] 同步员工准入失败：%s", exc)
            return 0

        for open_id, display_name, department in rows:
            normalized = str(open_id or "").strip()
            if not normalized:
                continue
            if self.state.remember_approved(
                normalized,
                approver="employee_directory",
                source="employee_directory",
            ):
                item = self.state.approved.get(normalized)
                if isinstance(item, dict):
                    item["display_name"] = str(display_name or "")
                    item["department"] = str(department or "")
                    self.state.save()
                synced += 1
        return synced

    def _audit_db_path(self) -> Path:
        project_root = getattr(self, "project_root", Path.cwd())
        return Path(project_root) / "data" / "ai_inbox.db"

    @staticmethod
    def _is_lark_event(event: AstrMessageEvent) -> bool:
        name = (event.get_platform_name() or "").lower()
        if name in {"lark", "feishu"}:
            return True
        platform_id = (event.get_platform_id() or "").lower()
        if any(token in platform_id for token in ("lark", "feishu", "飞书", "小助手")):
            return True
        sender_id = event.get_sender_id() or ""
        group_id = event.get_group_id() or ""
        msg = getattr(event, "message_obj", None)
        chat_id = str(getattr(msg, "chat_id", "") or "")
        raw_type = str(getattr(msg, "type", "") or "")
        return (
            sender_id.startswith("ou_")
            or group_id.startswith("oc_")
            or chat_id.startswith("oc_")
            or "FriendMessage" in raw_type
            or "GroupMessage" in raw_type
        )

    @staticmethod
    def _is_trusted_card_action(event: AstrMessageEvent) -> bool:
        msg = getattr(event, "message_obj", None)
        return bool(
            getattr(event, "is_card_action", False)
            or getattr(msg, "is_card_action", False)
        )

    @staticmethod
    def _reply(event: AstrMessageEvent, text: str) -> None:
        result = MessageEventResult().message(text).use_t2i(False).stop_event()
        event.set_result(result)

    def _format_status(self) -> str:
        return (
            "飞书通道治理状态\n"
            f"- enabled: {self.config.enabled}\n"
            f"- dm_policy: {self.config.dm_policy}\n"
            f"- group_policy: {self.config.group_policy}\n"
            f"- approved: {len(self.state.approved)}\n"
            f"- pending: {len(self.state.pending)}\n"
            f"- bindings: {len(self.state.bindings)}"
        )

    def _format_pairings(self) -> str:
        if not self.state.pending:
            return "当前没有待审批配对。"
        lines = ["待审批配对："]
        for code, item in sorted(self.state.pending.items()):
            lines.append(
                f"- {code}: {item.get('open_id', '')} expires={item.get('expires_at', '')}"
            )
        return "\n".join(lines)

    @staticmethod
    def _usage() -> str:
        return (
            "用法：\n"
            "/feishu-control status\n"
            "/feishu-control pairing list\n"
            "/feishu-control pairing approve <code>\n"
            "/feishu-control pairing revoke <open_id>\n"
            "/feishu-control bind user <open_id> <agent_id> [workspace]\n"
            "/feishu-control bind group <chat_id> <agent_id> [workspace]"
        )
