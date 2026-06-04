#!/usr/bin/env python3
# ruff: noqa: E402
"""轮询设计师需求调研多维表格 → 发现目标用户填了问卷 → 自动推送入职卡。

设计:
- 用 Q1 姓名字段匹配 target.name_aliases(支持名字变体: "黄柳泉" / "柳泉")
- marker 文件防重复推送: data/.onboarding_pushed_markers/{user_id}
- **临时任务自卸**: 所有 target 都推送过 → 调 install-onboarding-watch-cron.sh remove
  自动卸 cron。避免临时任务永久占用 cron 资源。
- cron 友好: 安静模式默认只输出关键事件(找到 / 推送 / 失败),无操作时只一行
- 多 target: TARGETS 是 list,后续要服众多个设计师只需追加(加完重装 cron)

用法:
    # 单次 check + 推送(cron 用):
    python scripts-tools/check_and_push_onboarding.py

    # dry-run(只查不推 + 不卸 cron):
    python scripts-tools/check_and_push_onboarding.py --dry-run

    # 不让脚本自动卸 cron(想保留轮询):
    python scripts-tools/check_and_push_onboarding.py --no-auto-uninstall

    # 详细日志:
    python scripts-tools/check_and_push_onboarding.py --verbose

cron(每 30 分钟,bash install-onboarding-watch-cron.sh install 自动装):
    */30 * * * * cd /Users/dianchi/DC-Agent && set -a && . ~/.dc-agent.env && set +a \\
        && DC_AGENT_ROOT=/Users/dianchi/DC-Agent \\
        /Users/dianchi/DC-Agent/.venv/bin/python \\
        scripts-tools/check_and_push_onboarding.py \\
        >> /Users/dianchi/DC-Agent/data/onboarding_watch.log 2>&1
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

DC_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DC_ROOT / "dc_engines"))

from dc_engines.card_runtime import send_card_via_runtime
from dc_engines.feishu_card_streamer import (
    FeishuCardStreamer,
    build_onboarding_dept_card,
)
from dc_engines.feishu_hub import get_client, is_enabled
from lark_oapi.api.bitable.v1 import ListAppTableRecordRequest

# ────────── 配置 ──────────

APP_TOKEN = "HBwpbGWNBazLdzsL9hkcUm8Hnkb"  # 设计师需求调研多维表格
TABLE_ID = "tblvU08w4cnh7pHz"
SURVEY_NAME_FIELD = (
    "Q1 你的姓名"  # Q1 字段名,跟 create_design_survey_bitable.py FIELDS[0] 对齐
)

MARKER_DIR = DC_ROOT / "data" / ".onboarding_pushed_markers"


@dataclass(frozen=True)
class Target:
    user_id: str  # 飞书 tenant user_id,用作 marker 和 IM 接收人
    to_name: str  # 推送入职卡时的称呼
    name_aliases: tuple[str, ...]  # Q1 字段可能的填写变体(全名/简称)


TARGETS: list[Target] = [
    Target(
        user_id="g62f2c35",
        to_name="柳泉",
        name_aliases=("黄柳泉", "柳泉"),
    ),
    # 后续要服更多设计师,在这里加 Target 即可
]


# ────────── 日志 ──────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("onboarding_watch")


# ────────── 工具函数 ──────────


def extract_q1_value(fields: dict) -> str:
    """飞书 bitable 文本字段可能返 str 或 [{type:'text', text:'...'}] 数组,统一抽出文本。"""
    raw = fields.get(SURVEY_NAME_FIELD)
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, list):
        # 数组形式: [{"type":"text", "text":"黄柳泉"}, ...]
        return "".join(
            item.get("text", "") if isinstance(item, dict) else str(item)
            for item in raw
        ).strip()
    return str(raw).strip()


def list_all_records(client) -> list[dict]:
    """分页拿全部 records。问卷规模小(< 1000 条),一页 500 通常一把搞定。"""
    records: list[dict] = []
    page_token: str | None = None
    while True:
        builder = (
            ListAppTableRecordRequest.builder()
            .app_token(APP_TOKEN)
            .table_id(TABLE_ID)
            .page_size(500)
            .field_names(json.dumps([SURVEY_NAME_FIELD], ensure_ascii=False))
        )
        if page_token:
            builder = builder.page_token(page_token)
        resp = client.bitable.v1.app_table_record.list(builder.build())
        if not resp.success():
            logger.error("list records 失败: code=%s msg=%s", resp.code, resp.msg)
            return records
        items = resp.data.items or []
        for r in items:
            records.append(
                {
                    "record_id": r.record_id,
                    "fields": r.fields or {},
                }
            )
        if not resp.data.has_more:
            break
        page_token = resp.data.page_token
    return records


async def push_onboarding_card(client, target: Target) -> bool:
    """以小助手身份给 target 发入职卡。成功返 True。"""
    card = build_onboarding_dept_card(welcome_name=target.to_name)
    streamer = FeishuCardStreamer(client)
    stream = await send_card_via_runtime(
        streamer,
        card_type="onboarding_department",
        chat_id=target.user_id,
        receive_id_type="user_id",
        card=card,
        platform_id="巅池-Agent小助手",
        event="start",
        detail="questionnaire onboarding watcher",
    )
    if stream is None:
        logger.error(
            "❌ 推送入职卡给 %s(%s) 失败",
            target.to_name,
            target.user_id,
        )
        return False
    logger.info(
        "✅ 已推送入职卡给 %s(%s) message_id=%s",
        target.to_name,
        target.user_id,
        stream.message_id,
    )
    return True


def marker_path(user_id: str) -> Path:
    return MARKER_DIR / user_id


def has_been_pushed(user_id: str) -> bool:
    return marker_path(user_id).exists()


def mark_pushed(user_id: str, record_id: str) -> None:
    MARKER_DIR.mkdir(parents=True, exist_ok=True)
    marker_path(user_id).write_text(
        json.dumps(
            {"user_id": user_id, "matched_record": record_id},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def uninstall_self_cron() -> None:
    """所有 target 都推过后,调 install sh 卸自己的 cron。

    临时任务做完就该让位,不应永久占 cron。marker 文件保留,如果有人手动重装
    cron,下次跑时会检测到 marker 立刻自卸,不会重复推。
    """
    sh = DC_ROOT / "scripts-tools" / "install-onboarding-watch-cron.sh"
    if not sh.exists():
        logger.warning("install sh 不存在,无法自动卸 cron: %s", sh)
        return
    try:
        result = subprocess.run(
            ["bash", str(sh), "remove"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            logger.info("🧹 已自动卸 cron(临时任务完成): %s", result.stdout.strip())
        else:
            logger.error(
                "卸 cron 失败 rc=%s stderr=%s", result.returncode, result.stderr
            )
    except Exception as exc:
        logger.error("卸 cron 异常: %s", exc)


# ────────── 主流程 ──────────


def main() -> int:
    p = argparse.ArgumentParser(description="问卷填表 → 入职卡推送 轮询器")
    p.add_argument(
        "--dry-run", action="store_true", help="只查不推 + 不写 marker + 不卸 cron"
    )
    p.add_argument(
        "--no-auto-uninstall",
        action="store_true",
        help="所有 target 都推完后不要自动卸 cron(默认会自卸)",
    )
    p.add_argument("--verbose", action="store_true", help="多打日志")
    args = p.parse_args()

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    if not is_enabled():
        logger.error("feishu hub 未启用。检查 DC_AGENT_ROOT + ~/.dc-agent.env")
        return 1

    # 先看是不是所有 target 都已推过 → 直接卸 cron 退出(不再调用任何 API)
    if all(has_been_pushed(t.user_id) for t in TARGETS):
        logger.info("所有 target(共 %d)已推过入职卡,临时任务完成", len(TARGETS))
        if not args.dry_run and not args.no_auto_uninstall:
            uninstall_self_cron()
        return 0

    client = get_client()
    assert client is not None

    records = list_all_records(client)
    logger.info("拉取多维表格记录: %d 条", len(records))

    if args.verbose:
        for r in records:
            logger.debug(
                "  record %s: Q1=%r", r["record_id"], extract_q1_value(r["fields"])
            )

    matched_count = 0
    for target in TARGETS:
        if has_been_pushed(target.user_id):
            if args.verbose:
                logger.debug("  %s 已推过(marker 存在),跳过", target.to_name)
            continue

        # 在 records 里找 Q1 姓名匹配
        match = None
        for r in records:
            q1 = extract_q1_value(r["fields"])
            if q1 in target.name_aliases:
                match = r
                break

        if match is None:
            logger.info("%s 还没填问卷,跳过", target.to_name)
            continue

        matched_count += 1
        logger.info(
            "🎯 %s 已填问卷(record_id=%s, Q1=%r),准备推送入职卡",
            target.to_name,
            match["record_id"],
            extract_q1_value(match["fields"]),
        )

        if args.dry_run:
            logger.info("  [dry-run] 跳过推送")
            continue

        if asyncio.run(push_onboarding_card(client, target)):
            mark_pushed(target.user_id, match["record_id"])

    if matched_count == 0:
        logger.info("本轮没有新的填表用户")

    # 本轮推送之后,再 check 一遍:如果推送让所有 target 都达成了 → 自卸 cron
    if all(has_been_pushed(t.user_id) for t in TARGETS):
        logger.info("推送后所有 target(共 %d)已达成,临时任务完成", len(TARGETS))
        if not args.dry_run and not args.no_auto_uninstall:
            uninstall_self_cron()
    return 0


if __name__ == "__main__":
    sys.exit(main())
