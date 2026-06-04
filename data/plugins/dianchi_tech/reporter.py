"""巅池-技术 09:30 汇报：飞书私聊蔡挺 + wiki 子页。

被 ``scripts-tools/dianchi-tech-report.sh`` 在 cron 09:30 调用。

流程：
1. 读 ``data/dianchi_tech/{DATE}/report.md``（夜间 agy 产出）
2. 飞书 IM：interactive card（markdown）发蔡挺
3. Wiki：在『DC-Agent 运维』空间下建子页（**空间需用户手动预先创建并把机器人加为成员**，
   因为 wiki space 创建只支持 user_access_token，机器人 tenant token 没权限自动建空间）
4. 写 ``delivery.json`` 记录推送状态

失败哲学：IM 是关键路径，wiki 是 best-effort。wiki 失败不影响 IM。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import date as _date
from pathlib import Path
from typing import Any

import lark_oapi as lark
from dc_engines.card_runtime import send_card_via_runtime
from dc_engines.feishu_card_streamer import FeishuCardStreamer
from lark_oapi.api.docx.v1 import (
    Block,
    CreateDocumentBlockChildrenRequest,
    CreateDocumentBlockChildrenRequestBody,
    CreateDocumentRequest,
    CreateDocumentRequestBody,
    Text,
    TextElement,
    TextRun,
)
from lark_oapi.api.wiki.v2 import (
    ListSpaceRequest,
    MoveDocsToWikiSpaceNodeRequest,
)
from lark_oapi.api.wiki.v2.model.move_docs_to_wiki_space_node_request_body import (
    MoveDocsToWikiSpaceNodeRequestBody,
)

try:
    from report_guard import guard_report
except Exception:  # noqa: BLE001
    guard_report = None

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [reporter] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


DC_ROOT = Path("/Users/dianchi/DC-Agent")
DATA_ROOT = DC_ROOT / "data" / "dianchi_tech"
DEFAULT_WIKI_SPACE_NAME = "DC-Agent 运维"

# 巅池-技术（DevOps）app —— 这个 plugin 专用的发送方
# secret 从 env DIANCHI_TECH_APP_SECRET 取，或回退到 cmd_config.json
DIANCHI_TECH_APP_ID = "cli_a978167822785bcb"


def _load_dianchi_tech_secret() -> str:
    """优先从 env，回退到 cmd_config.json 里 type=lark id=巅池-技术（DevOps） 的 app_secret。"""
    env_secret = os.environ.get("DIANCHI_TECH_APP_SECRET", "").strip()
    if env_secret:
        return env_secret
    try:
        cfg = json.loads(
            (DC_ROOT / "data" / "cmd_config.json").read_text(encoding="utf-8")
        )
        for p in cfg.get("platform", []):
            if (
                str(p.get("type", "")).lower() == "lark"
                and p.get("app_id") == DIANCHI_TECH_APP_ID
            ):
                return str(p.get("app_secret", "")).strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("从 cmd_config.json 读 secret 失败：%s", exc)
    return ""


def _build_client() -> lark.Client | None:
    """build 一个 reporter 专用的 lark.Client（不走 feishu_hub 单例，那个绑死的是 Agent小助手）"""
    secret = _load_dianchi_tech_secret()
    if not secret:
        return None
    return lark.Client.builder().app_id(DIANCHI_TECH_APP_ID).app_secret(secret).build()


# ─────────────────────── 报告读取 + 降级 ───────────────────────


def load_report(day_dir: Path, date_str: str) -> tuple[str, bool]:
    """返 (markdown, ok)。ok=False 时是降级文案，仍可发飞书让蔡挺看到。"""
    report = day_dir / "report.md"
    if report.exists() and report.stat().st_size > 0:
        if guard_report is not None:
            try:
                guard_result = guard_report(day_dir, date_str)
                logger.info("report_guard：%s", guard_result)
            except Exception as exc:  # noqa: BLE001
                logger.warning("report_guard 异常，继续读取原报告：%s", exc)
        return report.read_text(encoding="utf-8"), True

    run_meta_path = day_dir / "run.json"
    meta_hint = ""
    if run_meta_path.exists():
        try:
            meta = json.loads(run_meta_path.read_text(encoding="utf-8"))
            analysis = meta.get("analysis") or meta.get("claude") or {}
            attempts = analysis.get("attempts") or []
            attempt_hint = ""
            if isinstance(attempts, list) and attempts:
                parts = []
                for attempt in attempts:
                    if not isinstance(attempt, dict):
                        continue
                    parts.append(
                        "第{attempt}次={kind}/exit{exit}/{duration}s".format(
                            attempt=attempt.get("attempt", "?"),
                            kind=attempt.get("kind", "unknown"),
                            exit=attempt.get("exit", "?"),
                            duration=attempt.get("duration_seconds", "?"),
                        )
                    )
                if parts:
                    attempt_hint = "\n重试记录：" + "；".join(parts)
            meta_hint = (
                f"\n\n夜间任务退出码：agy={meta['agy']['exit']}, "
                f"analysis={analysis.get('exit', '?')}"
                f"\n阶段 B 总耗时：{analysis.get('duration_seconds', '?')}s"
                f"\n硬超时：{analysis.get('hard_timeout_seconds', '?')}s；"
                f"最多尝试：{analysis.get('max_attempts', '?')} 次"
                f"{attempt_hint}"
            )
        except Exception:  # noqa: BLE001
            pass
    text = (
        f"# 巅池-技术 日报 {date_str}\n\n"
        f"⚠️ 夜间任务未产出报告（report.md 缺失或为空）。{meta_hint}\n\n"
        f"请检查 `data/dianchi_tech/cron.log` 和 `{day_dir}/analysis_stdout.log`。"
    )
    return text, False


# ─────────────────────── 飞书 IM 推送 ───────────────────────


def _build_card(date_str: str, markdown: str, ok: bool) -> str:
    title = f"巅池-技术 日报 {date_str}" + ("" if ok else " ⚠️")
    if len(markdown) > 28000:  # 飞书 card 单 element 上限
        markdown = markdown[:27800] + "\n\n... (内容超长已截断，完整版见本地 / wiki)"
    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "green" if ok else "orange",
            "title": {"tag": "plain_text", "content": title},
        },
        "elements": [
            {"tag": "markdown", "content": markdown},
            {"tag": "hr"},
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": "自动推送 by dianchi_tech · 09:30 BJT",
                    }
                ],
            },
        ],
    }
    return json.dumps(card, ensure_ascii=False)


async def send_im(
    client,
    receive_id: str,
    date_str: str,
    markdown: str,
    ok: bool,
    *,
    id_type: str = "union_id",
) -> dict[str, Any]:
    """默认用 union_id（同开发者下所有 app 通用），open_id 是 per-app 的不能直接复用。"""
    if not receive_id or len(receive_id) < 15:
        return {"success": False, "error": "蔡挺 receive_id 未配置或不合法"}

    try:
        stream = await send_card_via_runtime(
            FeishuCardStreamer(client),
            card_type="daily_response",
            chat_id=receive_id,
            receive_id_type=id_type,
            card=json.loads(_build_card(date_str, markdown, ok)),
            platform_id="巅池-技术",
            event="start",
            detail="dianchi tech report card",
        )
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": f"{type(exc).__name__}: {exc}"}

    if stream is None:
        return {"success": False, "error": "send_card_failed"}
    msg_id = stream.message_id
    return {"success": True, "message_id": msg_id}


# ─────────────────────── Wiki（best-effort） ───────────────────────


async def _find_space_id(client, space_name: str) -> str | None:
    """在飞书 wiki 里找已存在的空间。机器人 tenant token 无权 CreateSpace，
    所以这里只读不建——如果找不到，提示用户手动建。"""
    try:
        page_token: str | None = None
        while True:
            req_builder = ListSpaceRequest.builder().page_size(50)
            if page_token:
                req_builder = req_builder.page_token(page_token)
            resp = await client.wiki.v2.space.alist(req_builder.build())
            if not resp.success() or not resp.data:
                logger.warning(
                    "list wiki space 失败 code=%s msg=%s", resp.code, resp.msg
                )
                return None
            for item in resp.data.items or []:
                if (item.name or "").strip() == space_name:
                    return item.space_id
            if not resp.data.has_more:
                return None
            page_token = resp.data.page_token
    except Exception as exc:  # noqa: BLE001
        logger.warning("find space 异常：%s", exc)
        return None


async def _create_empty_docx(client, title: str) -> str | None:
    try:
        body = CreateDocumentRequestBody.builder().title(title).build()
        req = CreateDocumentRequest.builder().request_body(body).build()
        resp = await client.docx.v1.document.acreate(req)
        if not resp.success() or not resp.data or not resp.data.document:
            logger.warning("create docx 失败 code=%s msg=%s", resp.code, resp.msg)
            return None
        return resp.data.document.document_id
    except Exception as exc:  # noqa: BLE001
        logger.warning("create docx 异常：%s", exc)
        return None


def _text_block(content: str) -> Block:
    """构造一个 text 块（block_type=2）。markdown 不会渲染，但保留原文。"""
    run = TextRun.builder().content(content).build()
    elem = TextElement.builder().text_run(run).build()
    text_body = Text.builder().elements([elem]).build()
    return Block.builder().block_type(2).text(text_body).build()


async def _write_markdown_into_docx(client, document_id: str, markdown: str) -> bool:
    """按段落切，每段一个 text block。块上限 5000 chars/element，留余地切 4500。"""
    MAX = 4500
    paragraphs: list[str] = []
    for para in markdown.split("\n"):
        if len(para) <= MAX:
            paragraphs.append(para)
        else:
            for i in range(0, len(para), MAX):
                paragraphs.append(para[i : i + MAX])

    # 飞书 API 单次 create_children 最多 50 个 block；分批
    BATCH = 50
    for batch_start in range(0, len(paragraphs), BATCH):
        batch = paragraphs[batch_start : batch_start + BATCH]
        children = [_text_block(p if p else " ") for p in batch]
        body = (
            CreateDocumentBlockChildrenRequestBody.builder()
            .children(children)
            .index(-1)  # -1 = append to end
            .build()
        )
        req = (
            CreateDocumentBlockChildrenRequest.builder()
            .document_id(document_id)
            .block_id(document_id)  # 根 block_id 等于 document_id
            .request_body(body)
            .build()
        )
        try:
            resp = await client.docx.v1.document_block_children.acreate(req)
        except Exception as exc:  # noqa: BLE001
            logger.warning("write docx batch 异常 batch=%d: %s", batch_start, exc)
            return False
        if not resp.success():
            logger.warning(
                "write docx batch 失败 batch=%d code=%s msg=%s",
                batch_start,
                resp.code,
                resp.msg,
            )
            return False
    return True


async def _move_docx_into_wiki(client, space_id: str, document_id: str) -> str | None:
    try:
        body = (
            MoveDocsToWikiSpaceNodeRequestBody.builder()
            .obj_type("docx")
            .obj_token(document_id)
            .build()
        )
        req = (
            MoveDocsToWikiSpaceNodeRequest.builder()
            .space_id(space_id)
            .request_body(body)
            .build()
        )
        resp = await client.wiki.v2.space_node.amove_docs_to_wiki(req)
        if not resp.success():
            logger.warning("move docx 到 wiki 失败 code=%s msg=%s", resp.code, resp.msg)
            return None
        if resp.data and getattr(resp.data, "wiki_token", None):
            return resp.data.wiki_token
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("move docx 到 wiki 异常：%s", exc)
        return None


async def publish_wiki(
    client, space_name: str, date_str: str, markdown: str
) -> dict[str, Any]:
    space_id = await _find_space_id(client, space_name)
    if not space_id:
        return {
            "success": False,
            "error": (
                f"找不到 wiki 空间 '{space_name}'。请在飞书手动建一个同名 wiki 空间，"
                "并把『巅池-技术（DevOps）』机器人加为成员（编辑权限），下次就能写入。"
            ),
        }

    title = f"巅池-技术 日报 {date_str}"
    doc_id = await _create_empty_docx(client, title)
    if not doc_id:
        return {"success": False, "error": "建 docx 失败"}

    body_ok = await _write_markdown_into_docx(client, doc_id, markdown)
    wiki_token = await _move_docx_into_wiki(client, space_id, doc_id)

    result: dict[str, Any] = {
        "success": bool(wiki_token),
        "space_id": space_id,
        "document_id": doc_id,
        "body_written": body_ok,
    }
    if wiki_token:
        result["wiki_token"] = wiki_token
        result["wiki_url"] = f"https://feishu.cn/wiki/{wiki_token}"
    else:
        result["error"] = "挂载 docx 到 wiki 失败（docx 已建好，在云盘根目录）"
    return result


# ─────────────────────── main ───────────────────────


FOLDER_NAME = "日常任务报告"
FILE_NAME_TPL = "巅池-技术日报-{date}.md"

DESKTOP_DIR = Path.home() / "Desktop" / FOLDER_NAME
NAS_DIR = Path("/Users/dianchi/nas_kb/inbox") / FOLDER_NAME


def _write_copy(target_dir: Path, date_str: str, markdown: str) -> dict:
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / FILE_NAME_TPL.format(date=date_str)
        path.write_text(markdown, encoding="utf-8")
        return {
            "success": True,
            "path": str(path),
            "bytes": len(markdown.encode("utf-8")),
        }
    except OSError as exc:
        return {"success": False, "error": f"{type(exc).__name__}: {exc}"}


def sync_to_desktop(date_str: str, markdown: str) -> dict:
    """蔡挺 Mac 桌面 / 日常任务报告 / 巅池-技术日报-{DATE}.md"""
    return _write_copy(DESKTOP_DIR, date_str, markdown)


def sync_to_nas(date_str: str, markdown: str) -> dict:
    """NAS inbox / 日常任务报告 / —— nas_sync watcher 会自动 ingest 进 AstrBot nas_knowledge KB。
    NAS 未挂载就静默跳过（不崩）。"""
    if not Path("/Users/dianchi/nas_kb").is_dir():
        return {
            "success": False,
            "skipped": True,
            "reason": "NAS 未挂载 /Users/dianchi/nas_kb",
        }
    return _write_copy(NAS_DIR, date_str, markdown)


async def run(date_str: str, cai_ting_open_id: str, wiki_space_name: str) -> int:
    day_dir = DATA_ROOT / date_str
    markdown, ok = load_report(day_dir, date_str)
    logger.info("报告加载：%s (%d chars, ok=%s)", day_dir, len(markdown), ok)

    client = _build_client()
    if client is None:
        logger.error(
            "巅池-技术 app 凭证未配置（env DIANCHI_TECH_APP_SECRET 或 cmd_config.json）"
        )
        return 1

    im_result = await send_im(client, cai_ting_open_id, date_str, markdown, ok)
    logger.info("IM 结果：%s", im_result)

    if ok:
        wiki_result = await publish_wiki(client, wiki_space_name, date_str, markdown)
    else:
        wiki_result = {"success": False, "skipped": True, "reason": "降级模式不发 wiki"}
    logger.info("Wiki 结果：%s", wiki_result)

    desktop_result = sync_to_desktop(date_str, markdown)
    logger.info("桌面落地：%s", desktop_result)

    nas_result = sync_to_nas(date_str, markdown)
    logger.info("NAS 同步（→ AstrBot nas_knowledge KB 自动 ingest）：%s", nas_result)

    delivery = {
        "date": date_str,
        "report_ok": ok,
        "im": im_result,
        "wiki": wiki_result,
        "desktop": desktop_result,
        "nas": nas_result,  # nas_sync watcher 会自动 ingest 进 AstrBot KB
        "local_raw": str(day_dir / "report.md"),  # plugin 数据目录原始路径
    }
    day_dir.mkdir(parents=True, exist_ok=True)
    (day_dir / "delivery.json").write_text(
        json.dumps(delivery, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return 0 if im_result.get("success") else 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--date", default=_date.today().isoformat(), help="YYYY-MM-DD, 默认今天"
    )
    parser.add_argument(
        "--union-id",
        default="",
        help="蔡挺 union_id（同开发者下 app 通用；优先用这个）",
    )
    parser.add_argument(
        "--open-id",
        default="",
        help="[已废弃，仅向后兼容] open_id 跨 app 不通用，请用 union-id",
    )
    parser.add_argument(
        "--space",
        default=DEFAULT_WIKI_SPACE_NAME,
        help="wiki 空间名（**需用户预先在飞书创建并把机器人加为成员**）",
    )
    args = parser.parse_args()
    receive_id = args.union_id or args.open_id
    return asyncio.run(run(args.date, receive_id, args.space))


if __name__ == "__main__":
    sys.exit(main())
