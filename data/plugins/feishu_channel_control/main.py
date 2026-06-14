"""OpenClaw-style Feishu/Lark channel governance for DC-Agent.

The plugin integrates at AstrBot's event layer. It does not replace the Lark
adapter and does not call Feishu APIs directly; API calls stay centralized in
``dc_engines.feishu_hub`` and existing Feishu feature plugins.
"""

from __future__ import annotations

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
        logger.info(
            "[feishu_channel_control] enabled=%s dm=%s group=%s state=%s",
            self.config.enabled,
            self.config.dm_policy,
            self.config.group_policy,
            self.state_path,
        )

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
        is_group = bool(group_id or chat_id.startswith("oc_")) or "Group" in str(
            getattr(raw_msg, "type", "")
        )
        peer_id = group_id or chat_id or sender_id
        return FeishuPeer(
            kind="group" if is_group else "direct",
            peer_id=peer_id,
            sender_id=sender_id,
            platform_id=event.get_platform_id() or "",
            message_text=(event.message_str or "").strip(),
            mentioned=bool(getattr(event, "is_at_or_wake_command", False)),
            trusted_card_action=self._is_trusted_card_action(event),
        )

    @staticmethod
    def _is_lark_event(event: AstrMessageEvent) -> bool:
        name = (event.get_platform_name() or "").lower()
        return name in {"lark", "feishu"}

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
