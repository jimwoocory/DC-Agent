"""Alert Channel 配置 —— 接收人 / 渠道开关 / 告警等级路由。

配置文件位置（优先级从高到低）：
1. $DC_AGENT_ALERT_CHANNEL_CONFIG 环境变量指定
2. /Users/dianchi/DC-Agent/data/config/alert_channel.yaml
3. 内置默认（接收人空、只发 macOS）

凭证来自 data/cmd_config.json 的 lark adapter（巅池-技术 默认作为告警 bot）。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DC_ROOT = Path("/Users/dianchi/DC-Agent")
DEFAULT_CONFIG_PATH = DC_ROOT / "data" / "config" / "alert_channel.yaml"
CMD_CONFIG_PATH = DC_ROOT / "data" / "cmd_config.json"


@dataclass(slots=True)
class Recipient:
    """告警接收人。"""

    open_id: str  # 在告警 bot（巅池-技术）app 下的 open_id
    name: str
    min_level: str = "warning"  # info / warning / critical


@dataclass(slots=True)
class AlertChannelConfig:
    """Alert Channel 总配置。"""

    # 告警 bot 凭证（用哪个飞书机器人推送，默认 巅池-技术）
    lark_app_id: str = ""
    lark_app_secret: str = ""

    # 接收人
    recipients: list[Recipient] = field(default_factory=list)

    # 渠道开关
    channels_enabled: dict[str, bool] = field(
        default_factory=lambda: {"macos": True, "lark": True}
    )

    # level → 走哪些渠道
    level_routing: dict[str, list[str]] = field(
        default_factory=lambda: {
            "info": ["macos"],
            "warning": ["macos", "lark"],
            "critical": ["macos", "lark"],
        }
    )

    # 失败 fallback
    fallback_to_macos_on_lark_error: bool = True

    @property
    def has_lark_credentials(self) -> bool:
        return bool(self.lark_app_id and self.lark_app_secret)

    @property
    def has_recipients_for_lark(self) -> bool:
        return len(self.recipients) > 0


def _read_lark_credentials_from_cmd_config(app_id: str) -> tuple[str, str] | None:
    """从 AstrBot 的 cmd_config.json 借用某个 lark adapter 的 app_secret。

    这样不用再单独维护一份凭证，跟 AstrBot 共用同一份配置。
    """
    if not CMD_CONFIG_PATH.exists():
        return None
    try:
        with CMD_CONFIG_PATH.open(encoding="utf-8-sig") as f:
            cfg = json.load(f)
        for p in cfg.get("platform", []):
            if p.get("type") == "lark" and p.get("app_id") == app_id:
                secret = p.get("app_secret", "")
                if secret:
                    return app_id, secret
    except (OSError, json.JSONDecodeError):
        pass
    return None


def load_config(path: str | Path | None = None) -> AlertChannelConfig:
    """加载配置。文件不存在时返回内置默认。"""
    path = Path(
        path
        or os.environ.get("DC_AGENT_ALERT_CHANNEL_CONFIG", "")
        or DEFAULT_CONFIG_PATH
    )

    cfg = AlertChannelConfig()
    if not path.exists():
        return cfg

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, OSError):
        return cfg

    # 取 lark_app_id，从 cmd_config.json 借 secret
    lark_app_id = str(raw.get("lark_app_id", "") or "").strip()
    if lark_app_id:
        cfg.lark_app_id = lark_app_id
        loaded = _read_lark_credentials_from_cmd_config(lark_app_id)
        if loaded:
            cfg.lark_app_id, cfg.lark_app_secret = loaded

    # 接收人
    raw_recipients = raw.get("recipients") or []
    if isinstance(raw_recipients, list):
        cfg.recipients = [
            Recipient(
                open_id=str(r.get("open_id", "")).strip(),
                name=str(r.get("name", "") or "").strip() or "<匿名>",
                min_level=str(r.get("min_level", "warning") or "warning"),
            )
            for r in raw_recipients
            if isinstance(r, dict) and r.get("open_id")
        ]

    # 渠道开关
    raw_channels = raw.get("channels_enabled") or {}
    if isinstance(raw_channels, dict):
        cfg.channels_enabled.update(
            {k: bool(v) for k, v in raw_channels.items() if isinstance(k, str)}
        )

    # level routing
    raw_routing = raw.get("level_routing") or {}
    if isinstance(raw_routing, dict):
        for lv, channels in raw_routing.items():
            if not isinstance(channels, list):
                continue
            cfg.level_routing[str(lv)] = [str(c) for c in channels]

    # fallback
    cfg.fallback_to_macos_on_lark_error = bool(
        raw.get("fallback_to_macos_on_lark_error", True)
    )

    return cfg
