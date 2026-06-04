from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class CommunicationChannelPolicy:
    default_channel: str
    allowed_channels: tuple[str, ...]
    blocked_defaults: tuple[str, ...]
    email_allowed_only_if_explicit: bool
    guidance: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "default_channel": self.default_channel,
            "allowed_channels": list(self.allowed_channels),
            "blocked_defaults": list(self.blocked_defaults),
            "email_allowed_only_if_explicit": self.email_allowed_only_if_explicit,
            "guidance": self.guidance,
        }


CLIENT_CHANNEL_POLICY = CommunicationChannelPolicy(
    default_channel="wechat_private_domain_or_feishu_message",
    allowed_channels=("微信私域", "微信群", "朋友圈", "飞书消息", "短信", "话术草稿"),
    blocked_defaults=("客户邮件", "外发邮件", "邮箱群发"),
    email_allowed_only_if_explicit=True,
    guidance=(
        "公司客户触达默认使用微信私域、微信群、朋友圈、飞书消息、短信或话术草稿；"
        "不要默认生成客户邮件、邮件主题或邮件正文。只有用户明确说“邮件/邮箱/email”时才输出邮件格式。"
    ),
)

GENERAL_CHANNEL_POLICY = CommunicationChannelPolicy(
    default_channel="work_message",
    allowed_channels=("飞书消息", "通知", "话术草稿", "文案草稿"),
    blocked_defaults=(),
    email_allowed_only_if_explicit=True,
    guidance="默认生成可复制的工作消息或文案草稿；邮件格式必须由用户明确要求。",
)

_EMAIL_RE = re.compile(r"(邮件|邮箱|email|mail)", re.IGNORECASE)


def communication_channel_policy_for(
    *,
    department_id: str,
    message_text: str,
) -> dict[str, Any]:
    policy = (
        CLIENT_CHANNEL_POLICY
        if department_id == "client_dept"
        else GENERAL_CHANNEL_POLICY
    )
    data = policy.to_dict()
    data["explicit_email_requested"] = bool(_EMAIL_RE.search(message_text or ""))
    data["should_use_email_format"] = (
        data["explicit_email_requested"]
        if data["email_allowed_only_if_explicit"]
        else True
    )
    if department_id == "client_dept" and not data["explicit_email_requested"]:
        data["instruction"] = (
            "按客户触达话术/私域消息输出，不要问是否需要邮件审查，不要生成邮件主题或邮件正文。"
        )
    else:
        data["instruction"] = data["guidance"]
    return data
