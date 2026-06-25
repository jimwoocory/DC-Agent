"""Feishu/Lark channel governance helpers for DC-Agent.

This module is intentionally framework-light so plugin behavior can be tested
without a running AstrBot instance or real Feishu credentials.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

Policy = str


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_id(value: Any) -> str:
    return str(value or "").strip()


@dataclass(slots=True)
class FeishuPeer:
    kind: str
    peer_id: str
    sender_id: str
    platform_id: str = ""
    message_text: str = ""
    mentioned: bool = False
    trusted_card_action: bool = False


@dataclass(slots=True)
class ChannelDecision:
    allowed: bool
    reason: str
    peer_kind: str = ""
    peer_id: str = ""
    agent_id: str = ""
    workspace: str = ""
    pairing_code: str = ""
    reply_text: str = ""
    stop_event: bool = False
    chat_entry_mode: str = ""
    identity_status: str = ""
    fallback_policy: str = ""

    def metadata(self) -> dict[str, str | bool]:
        return {
            "feishu_channel_allowed": self.allowed,
            "feishu_channel_peer_kind": self.peer_kind,
            "feishu_channel_peer_id": self.peer_id,
            "feishu_channel_agent_id": self.agent_id,
            "feishu_channel_workspace": self.workspace,
            "feishu_channel_policy_reason": self.reason,
            "dc_chat_entry_allowed": self.allowed,
            "dc_chat_entry_mode": self.chat_entry_mode
            or ("normal_chat" if self.allowed else "blocked"),
            "dc_chat_entry_identity_status": self.identity_status,
            "dc_chat_entry_reason": self.reason,
            "dc_chat_entry_fallback_policy": self.fallback_policy,
        }


@dataclass(slots=True)
class RouteBinding:
    agent_id: str = ""
    workspace: str = ""
    require_mention: bool | None = None
    allow_from: list[str] = field(default_factory=list)


@dataclass(slots=True)
class FeishuChannelConfig:
    enabled: bool = True
    dm_policy: Policy = "open"
    group_policy: Policy = "allowlist"
    allow_from: list[str] = field(default_factory=list)
    auto_approve_employee_directory: bool = True
    owners: list[str] = field(default_factory=list)
    group_allow_from: list[str] = field(default_factory=list)
    require_mention: bool = True
    trusted_card_actions_only: bool = True
    default_agent_id: str = "main"
    dynamic_agent_creation: bool = False
    workspace_template: str = "data/feishu_agents/{agent_id}"
    pairing_ttl_minutes: int = 30
    groups: dict[str, RouteBinding] = field(default_factory=dict)
    users: dict[str, RouteBinding] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> FeishuChannelConfig:
        data = raw or {}
        dynamic = data.get("dynamic_agent_creation")
        if isinstance(dynamic, dict):
            dynamic_enabled = bool(dynamic.get("enabled", False))
            workspace_template = str(
                dynamic.get("workspace_template")
                or data.get("workspace_template")
                or cls.workspace_template
            )
        else:
            dynamic_enabled = bool(
                dynamic or data.get("dynamic_agent_creation_enabled", False)
            )
            workspace_template = str(
                data.get("workspace_template") or cls.workspace_template
            )
        return cls(
            enabled=bool(data.get("enabled", True)),
            dm_policy=_policy(data.get("dm_policy", data.get("dmPolicy")), "open"),
            group_policy=_policy(
                data.get("group_policy", data.get("groupPolicy")), "allowlist"
            ),
            allow_from=_list(data.get("allow_from", data.get("allowFrom"))),
            auto_approve_employee_directory=bool(
                data.get(
                    "auto_approve_employee_directory",
                    data.get("autoApproveEmployeeDirectory", True),
                )
            ),
            owners=_list(data.get("owners")),
            group_allow_from=_list(
                data.get("group_allow_from", data.get("groupAllowFrom"))
            ),
            require_mention=bool(
                data.get("require_mention", data.get("requireMention", True))
            ),
            trusted_card_actions_only=bool(data.get("trusted_card_actions_only", True)),
            default_agent_id=str(data.get("default_agent_id") or "main"),
            dynamic_agent_creation=dynamic_enabled,
            workspace_template=workspace_template,
            pairing_ttl_minutes=int(data.get("pairing_ttl_minutes") or 30),
            groups=_bindings(data.get("groups")),
            users=_bindings(data.get("users")),
        )


class FeishuChannelState:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.approved: dict[str, dict[str, Any]] = {}
        self.pending: dict[str, dict[str, Any]] = {}
        self.bindings: dict[str, dict[str, Any]] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        self.approved = _dict(raw.get("approved"))
        self.pending = _dict(raw.get("pending"))
        self.bindings = _dict(raw.get("bindings"))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "approved": self.approved,
            "pending": self.pending,
            "bindings": self.bindings,
        }
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def is_approved(self, open_id: str) -> bool:
        return open_id in self.approved

    def remember_approved(
        self,
        open_id: str,
        *,
        approver: str = "",
        source: str = "auto",
    ) -> bool:
        open_id = normalize_id(open_id)
        if not open_id or open_id in self.approved:
            return False
        self.approved[open_id] = {
            "approved_at": utc_now().isoformat(),
            "approver": approver,
            "source": source,
        }
        self.save()
        return True

    def create_pairing_code(
        self,
        open_id: str,
        *,
        now: datetime | None = None,
        ttl_minutes: int = 30,
    ) -> str:
        current = now or utc_now()
        self.expire_pending(current)
        for code, item in self.pending.items():
            if item.get("open_id") == open_id:
                return code
        code = secrets.token_hex(3).upper()
        while code in self.pending:
            code = secrets.token_hex(3).upper()
        expires_at = current + timedelta(minutes=ttl_minutes)
        self.pending[code] = {
            "open_id": open_id,
            "created_at": current.isoformat(),
            "expires_at": expires_at.isoformat(),
        }
        self.save()
        return code

    def approve_pairing(self, code: str, *, approver: str = "") -> str | None:
        normalized = code.strip().upper()
        item = self.pending.pop(normalized, None)
        if not isinstance(item, dict):
            return None
        open_id = normalize_id(item.get("open_id"))
        if not open_id:
            self.save()
            return None
        self.approved[open_id] = {
            "approved_at": utc_now().isoformat(),
            "approver": approver,
            "source": "pairing",
        }
        self.save()
        return open_id

    def revoke(self, open_id: str) -> bool:
        existed = self.approved.pop(open_id, None) is not None
        if existed:
            self.save()
        return existed

    def set_binding(self, key: str, binding: RouteBinding) -> None:
        self.bindings[key] = {
            "agent_id": binding.agent_id,
            "workspace": binding.workspace,
            "require_mention": binding.require_mention,
            "allow_from": binding.allow_from,
        }
        self.save()

    def get_binding(self, key: str) -> RouteBinding | None:
        return _binding(self.bindings.get(key))

    def expire_pending(self, now: datetime | None = None) -> None:
        current = now or utc_now()
        expired = []
        for code, item in self.pending.items():
            try:
                expires_at = datetime.fromisoformat(str(item.get("expires_at")))
            except ValueError:
                expired.append(code)
                continue
            if expires_at <= current:
                expired.append(code)
        for code in expired:
            self.pending.pop(code, None)
        if expired:
            self.save()


class FeishuChannelController:
    def __init__(
        self,
        config: FeishuChannelConfig,
        state: FeishuChannelState,
    ) -> None:
        self.config = config
        self.state = state

    def decide(self, peer: FeishuPeer) -> ChannelDecision:
        if not self.config.enabled:
            return self._block(peer, "disabled")
        if (
            self.config.trusted_card_actions_only
            and peer.message_text.startswith("__card_action__:")
            and not peer.trusted_card_action
        ):
            return self._block(
                peer,
                "forged_card_action",
                reply=False,
                chat_entry_mode="sensitive_action_candidate",
                fallback_policy="fail_closed",
            )
        if peer.kind == "direct":
            return self._decide_direct(peer)
        if peer.kind == "group":
            return self._decide_group(peer)
        return self._block(peer, "unsupported_peer")

    def _decide_direct(self, peer: FeishuPeer) -> ChannelDecision:
        policy = self.config.dm_policy
        if policy == "disabled":
            return self._block(peer, "dm_disabled")
        is_static_allowed = self._is_static_allowed(peer.sender_id)
        if is_static_allowed or policy == "open":
            source = "dm_static_allow" if is_static_allowed else "dm_open_auto"
            self.state.remember_approved(peer.sender_id, source=source)
            return self._allow(
                peer,
                "dm_allowed",
                identity_status=self._identity_status(peer.sender_id),
                fallback_policy="normal_chat",
            )
        if policy == "allowlist":
            return self._block(peer, "dm_not_allowlisted")
        if policy == "pairing":
            if self.state.is_approved(peer.sender_id):
                return self._allow(peer, "dm_pairing_approved")
            code = self.state.create_pairing_code(
                peer.sender_id,
                ttl_minutes=self.config.pairing_ttl_minutes,
            )
            return ChannelDecision(
                allowed=False,
                reason="dm_pairing_required",
                peer_kind=peer.kind,
                peer_id=peer.peer_id,
                pairing_code=code,
                reply_text=(
                    "需要管理员批准后才能使用飞书小助手。\n"
                    f"配对码：{code}\n"
                    f"管理员执行：/feishu-control pairing approve {code}"
                ),
                stop_event=True,
                chat_entry_mode="normal_chat",
                identity_status="unknown",
                fallback_policy="fail_closed_explicit_pairing",
            )
        return self._block(peer, "dm_policy_invalid")

    def _decide_group(self, peer: FeishuPeer) -> ChannelDecision:
        policy = self.config.group_policy
        binding = self._binding_for(peer)
        if policy == "disabled":
            return self._block(peer, "group_disabled", reply=False)
        admitted = policy == "open" or peer.peer_id in self.config.group_allow_from
        admitted = admitted or binding is not None
        if not admitted:
            return self._block(peer, "group_not_allowlisted", reply=False)
        if binding and binding.allow_from and peer.sender_id not in binding.allow_from:
            return self._block(peer, "group_sender_not_allowlisted", reply=False)
        require_mention = self.config.require_mention
        if binding and binding.require_mention is not None:
            require_mention = binding.require_mention
        if require_mention and not peer.mentioned:
            return self._block(peer, "group_mention_required", reply=False)
        return self._allow(peer, "group_allowed", binding=binding)

    def _allow(
        self,
        peer: FeishuPeer,
        reason: str,
        *,
        binding: RouteBinding | None = None,
        identity_status: str = "verified",
        fallback_policy: str = "normal_chat",
    ) -> ChannelDecision:
        active_binding = binding or self._binding_for(peer)
        agent_id = active_binding.agent_id if active_binding else ""
        workspace = active_binding.workspace if active_binding else ""
        if not agent_id:
            if self.config.dynamic_agent_creation and peer.kind == "direct":
                agent_id = f"feishu-{peer.sender_id}"
            else:
                agent_id = self.config.default_agent_id
        if (
            not workspace
            and self.config.dynamic_agent_creation
            and peer.kind == "direct"
        ):
            workspace = self.config.workspace_template.format(
                agentId=agent_id,
                agent_id=agent_id,
                userId=peer.sender_id,
                user_id=peer.sender_id,
            )
        return ChannelDecision(
            allowed=True,
            reason=reason,
            peer_kind=peer.kind,
            peer_id=peer.peer_id,
            agent_id=agent_id,
            workspace=workspace,
            chat_entry_mode="normal_chat",
            identity_status=identity_status,
            fallback_policy=fallback_policy,
        )

    def _block(
        self,
        peer: FeishuPeer,
        reason: str,
        *,
        reply: bool = True,
        chat_entry_mode: str = "blocked",
        identity_status: str = "",
        fallback_policy: str = "fail_closed",
    ) -> ChannelDecision:
        return ChannelDecision(
            allowed=False,
            reason=reason,
            peer_kind=peer.kind,
            peer_id=peer.peer_id,
            reply_text="当前飞书入口未开放，请联系管理员。" if reply else "",
            stop_event=True,
            chat_entry_mode=chat_entry_mode,
            identity_status=identity_status,
            fallback_policy=fallback_policy,
        )

    def _is_static_allowed(self, open_id: str) -> bool:
        allowed = set(self.config.allow_from) | set(self.config.owners)
        return "*" in allowed or open_id in allowed

    def _binding_for(self, peer: FeishuPeer) -> RouteBinding | None:
        key = f"{peer.kind}:{peer.peer_id}"
        state_binding = self.state.get_binding(key)
        if state_binding is not None:
            return state_binding
        if peer.kind == "direct":
            return self.config.users.get(peer.peer_id)
        if peer.kind == "group":
            return self.config.groups.get(peer.peer_id)
        return None

    def _identity_status(self, open_id: str) -> str:
        item = self.state.approved.get(open_id)
        if not isinstance(item, dict):
            return "unknown"
        source = str(item.get("source") or "")
        if source in {"employee_directory", "pairing", "dm_static_allow"}:
            return "verified"
        return "partial"


def _policy(value: Any, default: str) -> str:
    text = str(value or default).strip().lower()
    if text in {"open", "allowlist", "pairing", "disabled"}:
        return text
    return default


def _list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list | tuple | set):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _bindings(value: Any) -> dict[str, RouteBinding]:
    if not isinstance(value, dict):
        return {}
    result = {}
    for key, raw in value.items():
        binding = _binding(raw)
        if binding is not None:
            result[str(key)] = binding
    return result


def _binding(value: Any) -> RouteBinding | None:
    if not isinstance(value, dict):
        return None
    require_mention = value.get("require_mention", value.get("requireMention"))
    return RouteBinding(
        agent_id=str(value.get("agent_id", value.get("agentId", "")) or ""),
        workspace=str(value.get("workspace", "") or ""),
        require_mention=(
            bool(require_mention) if require_mention is not None else None
        ),
        allow_from=_list(value.get("allow_from", value.get("allowFrom"))),
    )


__all__ = [
    "ChannelDecision",
    "FeishuChannelConfig",
    "FeishuChannelController",
    "FeishuChannelState",
    "FeishuPeer",
    "RouteBinding",
]
