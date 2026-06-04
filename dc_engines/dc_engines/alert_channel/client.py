"""Alert Channel 主客户端 —— 同步 / 异步 API + macOS / lark pusher。

调用方式：

    from dc_engines.alert_channel import send_alert
    send_alert(title='xxx', body='yyy', level='critical')

或从 bash（看门狗）：

    .venv/bin/python -m dc_engines.alert_channel \\
        --title "🚨 hermes 挂了" --body "诊断..." --level critical
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import subprocess
import sys
from dataclasses import dataclass, field
from enum import Enum

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
)

from .config import AlertChannelConfig, Recipient, load_config

logger = logging.getLogger("dc_engines.alert_channel")


class AlertLevel(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


_LEVEL_ORDER = {AlertLevel.INFO: 0, AlertLevel.WARNING: 1, AlertLevel.CRITICAL: 2}
_EMOJI = {
    AlertLevel.INFO: "ℹ️",
    AlertLevel.WARNING: "⚠️",
    AlertLevel.CRITICAL: "🚨",
}


@dataclass(slots=True)
class AlertResult:
    """单次告警的多渠道发送结果汇总。"""

    sent_to: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def success(self) -> bool:
        return bool(self.sent_to)

    def __str__(self) -> str:
        if self.success and not self.errors:
            return f"sent → {', '.join(self.sent_to)}"
        if self.success and self.errors:
            return f"sent → {', '.join(self.sent_to)} (部分失败: {self.errors})"
        return f"全部失败: {self.errors}"


# ────────────────────────── 内部 push 实现 ──────────────────────────


def _push_macos(title: str, body: str, subtitle: str = "") -> None:
    """通过 osascript 弹 macOS 通知（同步、本机）。"""
    title_esc = title.replace('"', '\\"').replace("\\", "\\\\")
    body_esc = body.replace('"', '\\"').replace("\\", "\\\\")
    sub_esc = subtitle.replace('"', '\\"').replace("\\", "\\\\")
    parts = [f'display notification "{body_esc}"', f'with title "{title_esc}"']
    if sub_esc:
        parts.append(f'subtitle "{sub_esc}"')
    script = " ".join(parts)
    subprocess.run(
        ["/usr/bin/osascript", "-e", script],
        check=False,
        capture_output=True,
        timeout=5,
    )


async def _push_lark(
    cfg: AlertChannelConfig,
    recipient: Recipient,
    title: str,
    body: str,
    level: AlertLevel,
) -> None:
    """通过指定的 lark app（默认巅池-技术）私聊推送给一个接收人。"""
    client = (
        lark.Client.builder()
        .app_id(cfg.lark_app_id)
        .app_secret(cfg.lark_app_secret)
        .log_level(lark.LogLevel.WARNING)
        .build()
    )

    # 飞书 interactive 卡片
    emoji = _EMOJI.get(level, "🔔")
    color = {"info": "blue", "warning": "yellow", "critical": "red"}.get(
        level.value, "blue"
    )
    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": color,
            "title": {"tag": "plain_text", "content": f"{emoji} {title}"},
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": body[:3000]}},
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": f"等级 {level.value} · 来自 DC-Agent alert_channel",
                    }
                ],
            },
        ],
    }

    body_obj = (
        CreateMessageRequestBody.builder()
        .receive_id(recipient.open_id)
        .msg_type("interactive")
        .content(json.dumps(card, ensure_ascii=False))
        .build()
    )
    req = (
        CreateMessageRequest.builder()
        .receive_id_type("open_id")
        .request_body(body_obj)
        .build()
    )
    resp = await client.im.v1.message.acreate(req)
    if not resp.success():
        raise RuntimeError(f"lark message.create 失败 code={resp.code} msg={resp.msg}")


# ────────────────────────── 主 API（异步）──────────────────────────


def _level_ge(level: AlertLevel, min_level: str) -> bool:
    """level 是否 >= min_level。"""
    try:
        min_lv = AlertLevel(min_level)
    except ValueError:
        min_lv = AlertLevel.WARNING
    return _LEVEL_ORDER[level] >= _LEVEL_ORDER[min_lv]


async def send_alert_async(
    *,
    title: str,
    body: str,
    level: str | AlertLevel = AlertLevel.WARNING,
    config: AlertChannelConfig | None = None,
) -> AlertResult:
    """异步发送告警到所有配置的渠道。"""
    if isinstance(level, str):
        try:
            level = AlertLevel(level)
        except ValueError:
            level = AlertLevel.WARNING

    cfg = config or load_config()
    result = AlertResult()

    channels_for_level = cfg.level_routing.get(level.value, ["macos"])

    # macOS
    if "macos" in channels_for_level and cfg.channels_enabled.get("macos", True):
        try:
            _push_macos(title, body[:200], subtitle=f"level={level.value}")
            result.sent_to.append("macos")
        except Exception as exc:  # noqa: BLE001
            result.errors["macos"] = str(exc)

    # Lark
    if "lark" in channels_for_level and cfg.channels_enabled.get("lark", True):
        if not cfg.has_lark_credentials:
            result.errors["lark"] = "凭证缺失（lark_app_id / app_secret 没配）"
        elif not cfg.has_recipients_for_lark:
            result.errors["lark"] = "接收人列表为空"
        else:
            # 按接收人各自 min_level 过滤
            targets = [r for r in cfg.recipients if _level_ge(level, r.min_level)]
            if not targets:
                result.errors["lark"] = f"没有接收人 min_level <= {level.value}"
            else:
                sent_any = False
                for r in targets:
                    try:
                        await _push_lark(cfg, r, title, body, level)
                        sent_any = True
                        logger.info(
                            "[alert_channel] lark 推送成功 to %s (%s)",
                            r.name,
                            r.open_id[:8],
                        )
                    except Exception as exc:  # noqa: BLE001
                        result.errors[f"lark:{r.name}"] = str(exc)
                if sent_any:
                    result.sent_to.append("lark")

    # Fallback：lark 全部失败 → 再发一次 macOS 作为兜底
    if (
        "lark" in channels_for_level
        and cfg.fallback_to_macos_on_lark_error
        and any(k.startswith("lark") for k in result.errors)
        and "macos" not in result.sent_to
    ):
        try:
            _push_macos(
                f"{title} [飞书推送失败]",
                f"{body[:150]}\n（飞书发送失败，看 alert_channel log）",
                subtitle=f"level={level.value}",
            )
            result.sent_to.append("macos[fallback]")
        except Exception as exc:  # noqa: BLE001
            result.errors["macos[fallback]"] = str(exc)

    return result


def send_alert(
    *,
    title: str,
    body: str,
    level: str | AlertLevel = AlertLevel.WARNING,
    config: AlertChannelConfig | None = None,
) -> AlertResult:
    """同步发送告警。内部跑 asyncio.run（bash 调用最方便）。"""
    return asyncio.run(
        send_alert_async(title=title, body=body, level=level, config=config)
    )


# ────────────────────────── CLI（bash 调用入口）──────────────────────────


def _cli() -> int:
    parser = argparse.ArgumentParser(
        description="DC-Agent alert_channel CLI · 发告警到 macOS / 飞书 / ..."
    )
    parser.add_argument("--title", required=True)
    parser.add_argument("--body", default="")
    parser.add_argument(
        "--level",
        default="warning",
        choices=["info", "warning", "critical"],
    )
    parser.add_argument("--config", default=None, help="可选：指定配置文件路径")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    if not args.quiet:
        logging.basicConfig(level=logging.INFO, format="%(message)s")

    cfg = load_config(args.config) if args.config else load_config()
    result = send_alert(title=args.title, body=args.body, level=args.level, config=cfg)

    if not args.quiet:
        print(result)
    return 0 if result.success else 1


if __name__ == "__main__":
    sys.exit(_cli())
