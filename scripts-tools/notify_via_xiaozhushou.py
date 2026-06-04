#!/usr/bin/env python3
# ruff: noqa: E402
"""用「巅池-Agent小助手」app(cli_aa8cc8***)给指定用户发卡片私信。

支持两种卡片:
- survey:     设计师需求调研问卷卡(本项目专用)
- onboarding: employee_onboarding plugin 的第一张入职卡(部门选择)
              发出去后,黄柳泉点按钮 → plugin 接管 state machine
              → 后续 role/name/教程/quiz 自动流转

用法:
    # 自测发问卷卡(发给蔡挺自己):
    python scripts-tools/notify_via_xiaozhushou.py \\
        --card-type survey \\
        --receive-id-type union_id \\
        --receive-id on_d02f744ffca7d68eac1afee00d7edb71 \\
        --to-name 蔡挺

    # 给黄柳泉发入职卡:
    python scripts-tools/notify_via_xiaozhushou.py \\
        --card-type onboarding \\
        --receive-id-type email \\
        --receive-id hlq@example.com \\
        --to-name 柳泉

    # dry-run:
    python scripts-tools/notify_via_xiaozhushou.py \\
        --card-type onboarding --receive-id-type email \\
        --receive-id x@y.com --to-name x --dry-run

权限要求:
    - im:message  以应用身份发消息

receive_id_type 选项:
    - email:    最简单,需要对方公司邮箱
    - union_id: 跨 app 通用
    - user_id:  企业内 tenant 范围
    - open_id:  app 范围内
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

DC_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DC_ROOT / "dc_engines"))

from dc_engines.card_runtime import send_card_via_runtime
from dc_engines.feishu_card_streamer import (
    FeishuCardStreamer,
    build_onboarding_dept_card,
)
from dc_engines.feishu_hub import get_client, is_enabled

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("notify")


DEFAULT_FORM_URL = (
    "https://o0ain5w98jh.feishu.cn/share/base/shrcnIUegEq9Ir97jmwzHTDjPLg"
)


def build_onboarding_card(to_name: str) -> str:
    """employee_onboarding plugin 入职流程的第一张卡(部门选择)。

    黄柳泉点按钮后,飞书会 callback 到 AstrBot,plugin 监听到 select_dept
    action 自动接管,继续推角色卡、姓名 prompt、教程清单。
    """
    card = build_onboarding_dept_card(welcome_name=to_name)
    return json.dumps(card, ensure_ascii=False)


def build_survey_card(to_name: str, form_url: str) -> str:
    """飞书 interactive 卡片 JSON,题目链接做成蓝色按钮,体验比纯文本好。"""
    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {
                "tag": "plain_text",
                "content": "🎨 设计师需求调研(8 分钟)",
            },
            "template": "blue",
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"**{to_name},你好~**\n\n"
                        "我们在调研「飞书里搞个**设计助手**能帮设计师"
                        "干掉哪些重复活儿(批量处理、抠图、找素材、加水印…)」。\n\n"
                        "这份问卷会决定我们**先做哪个功能**,所以你的真实想法非常重要。\n\n"
                        "⚠️ **文字题越具体越好**(比如「做某项目时一次要导 200 张图,"
                        "每张手工存」),我们才能做出真的帮上你的工具。"
                    ),
                },
            },
            {"tag": "hr"},
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {
                            "tag": "plain_text",
                            "content": "🚀 立即填写(约 8 分钟)",
                        },
                        "type": "primary",
                        "url": form_url,
                    }
                ],
            },
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": "有疑问随时找蔡挺~  填完数据会自动汇总到我们的多维表格",
                    }
                ],
            },
        ],
    }
    return json.dumps(card, ensure_ascii=False)


def main() -> int:
    p = argparse.ArgumentParser(description="用 Agent 小助手给指定用户发卡片")
    p.add_argument(
        "--card-type",
        required=True,
        choices=["survey", "onboarding"],
        help="survey=设计师需求调研问卷卡; onboarding=employee_onboarding 入职流程第一张卡",
    )
    p.add_argument(
        "--receive-id-type",
        required=True,
        choices=["email", "union_id", "user_id", "open_id"],
    )
    p.add_argument("--receive-id", required=True, help="接收人 ID(类型对应上一项)")
    p.add_argument("--to-name", required=True, help="接收人称呼(用在文案开头)")
    p.add_argument(
        "--form-url",
        default=DEFAULT_FORM_URL,
        help=f"survey 卡的表单链接(默认:{DEFAULT_FORM_URL})",
    )
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    if not is_enabled():
        logger.error("feishu hub 未启用。检查 DC_AGENT_ROOT + ~/.dc-agent.env")
        return 1

    if args.card_type == "survey":
        content = build_survey_card(args.to_name, args.form_url)
    else:
        content = build_onboarding_card(args.to_name)
    logger.info("卡片类型: %s", args.card_type)
    logger.info(
        "发送目标: type=%s, id=%s, name=%s",
        args.receive_id_type,
        args.receive_id,
        args.to_name,
    )
    if args.card_type == "survey":
        logger.info("表单链接: %s", args.form_url)
    logger.info("卡片预览(JSON 前 300 字符):")
    logger.info("  %s...", content[:300])

    if args.dry_run:
        logger.info("--dry-run,跳过 API 调用")
        return 0

    client = get_client()
    streamer = FeishuCardStreamer(client)
    stream = asyncio.run(
        send_card_via_runtime(
            streamer,
            card_type=(
                "onboarding_department"
                if args.card_type == "onboarding"
                else "daily_response"
            ),
            chat_id=args.receive_id,
            receive_id_type=args.receive_id_type,
            card=json.loads(content),
            platform_id="巅池-Agent小助手",
            event="start",
            detail=f"manual notify via xiaozhushou: {args.card_type}",
        )
    )
    if stream is None:
        logger.error("❌ 发送失败")
        return 2

    logger.info("✅ 已发送")
    logger.info(
        "  message_id=%s, chat_id=%s",
        stream.message_id,
        stream.chat_id,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
