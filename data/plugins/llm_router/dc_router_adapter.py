# ruff: noqa: E402, I001
"""dc-router adapter for AstrBot plugin.

边界层：把 AstrBot 的 AstrMessageEvent ↔ dc_router.MessageEnvelope 互转，
并把 dc_router.RouterDecision 翻译成 AstrBot 的实际动作（set_provider / 后续派发）。

设计原则：
- dc_router/ 和 harness/ 包跟 AstrBot 完全解耦（router/__init__.py 注释）
- adapter 是 plugin <-> router 包的唯一接口
- 任何异常都让上层 fallback 到 v1.0，不抛出
- 步骤 3 只实现 depth=DIRECT 路径（answer / preprocess）
- depth=FRONT (前端配额队列) 和 depth=HERMES (深度任务派发) → 步骤 4 接入
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import sys
import time
from pathlib import Path

# 让 `from dc_router import ...` 和 `from harness import ...` 能从 DC-Agent 顶层 import
_DC_AGENT_ROOT = Path(__file__).resolve().parents[3]
if str(_DC_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_DC_AGENT_ROOT))

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, MessageEventResult
from astrbot.api.message_components import File, Image, Plain, Record, Video
from astrbot.core.provider.entities import ProviderType
from astrbot.core.utils.media_utils import (
    IMAGE_COMPRESS_DEFAULT_MAX_SIZE,
    IMAGE_COMPRESS_DEFAULT_QUALITY,
    compress_image,
)


CLI_PROVIDER_PREFIX = "cli/"
ANTIGRAVITY_PROVIDER_ID = "cli/antigravity/gemini-3.5-flash"
ANTIGRAVITY_RESOURCE_KEY = "antigravity_cli_flash"
ANTIGRAVITY_COOLDOWN_SECONDS = 5
ANTIGRAVITY_PRIMARY_CHANNEL_NAME = "巅巅小助手"
ANTIGRAVITY_FALLBACK_CHANNEL_NAME = "池池小助手"
MULTIMODAL_PREPROCESS_PROVIDER_ID = "aihubmix/gemini-3.5-flash"
MULTIMODAL_PREPROCESS_FALLBACK_PROVIDER_ID = "aihubmix/gemini-3.1-pro-preview"
ANTIGRAVITY_FALLBACK_PROVIDER_ID = "aihubmix/gemini-3.5-flash"
GROK_BUILD_PROVIDER_ID = "cli/grok-build"
GROK_BUILD_FALLBACK_PROVIDER_ID = "aihubmix/grok-4.3"
CLI_QUEUE_OAUTH_FALLBACK_PROVIDER_ID = "codex/gpt-5.5-xhigh"
CLI_FAILURE_DIAG_MODEL = "gpt-5.4"
CLI_FAILURE_DIAG_TIMEOUT_SECONDS = 90

# ─────────── 飞书 wiki/docx URL inline fetch (2026-05-20 加) ───────────
# CLI providers 拿不到飞书 URL 的内容（需要 lark bot 身份调 API）。
# 检测消息里的飞书 URL → 用 lark_oapi 抓正文 → 替换成 <feishu_doc>...</feishu_doc> 块塞进 prompt。
_FEISHU_URL_RE = re.compile(
    r"https?://[^\s]*?feishu\.cn/(wiki|docx|docs)/([A-Za-z0-9_-]+)"
)
_FEISHU_CREDS_PATH = Path("/Users/dianchi/DC-Agent/data/feishu_whitelist.yaml")
_FEISHU_DOC_MAX_CHARS = 8000  # 单个文档塞进 prompt 的截断阈值
MULTIMODAL_PREPROCESS_PROMPT = """\
请用中文提取用户附件里的关键信息，供后续模型继续完成用户任务。
要求：
1. 如果是截图或图片，优先做 OCR，保留文字、数字、表格、按钮、错误信息和关键视觉信息。
2. 如果是营销/品牌素材，提炼主题、对象、语气、卖点和可用洞察。
3. 如果信息不确定，明确写“无法确认”，不要编造。
4. 只输出附件摘要，不要回答用户最终任务。
"""
_CLASSIFIER_JSON_RE = re.compile(r"\{[^{}]*\}", re.DOTALL)
_QUEUE_RECOVERY_TASK: asyncio.Task | None = None
_LOCAL_CASUAL_ACKS = {
    "嗯",
    "嗯嗯",
    "哦",
    "哦哦",
    "啊",
    "哈哈",
    "哈哈哈",
    "hh",
    "hhh",
    "6",
    "66",
    "666",
    "ok",
    "okay",
    "收到",
    "好的",
    "好",
    "行",
    "可以",
}


def _is_local_casual_ack(text: str, *, has_attachments: bool) -> bool:
    if has_attachments:
        return False
    normalized = re.sub(r"[\s，。！？!?,.、~～…]+", "", text).lower()
    return (
        bool(normalized) and len(normalized) <= 8 and normalized in _LOCAL_CASUAL_ACKS
    )


def _format_wait_minutes_from_eta(eta_at: float | None) -> str:
    if not eta_at:
        return "稍后"
    minutes = max(1, math.ceil((eta_at - time.time()) / 60))
    return f"约 {minutes} 分钟"


def _antigravity_queue_message(
    *,
    queue_position: int,
    eta_at: float | None,
    allow_fallback_choice: bool = True,
) -> str:
    fallback_line = (
        f"\n\n如果您不想排队等待，也可以选择「{ANTIGRAVITY_FALLBACK_CHANNEL_NAME}」"
        "先帮您处理。"
        if allow_fallback_choice
        else ""
    )
    return (
        "哎呀，现在小助手太忙啦，当前同时接待人数已经超过 9 人。"
        "我已经帮您排好队了，请您耐心等待⌛️\n\n"
        f"当前位置：第 {queue_position} 位\n"
        f"预计等待：{_format_wait_minutes_from_eta(eta_at)}\n"
        f"当前通道：{ANTIGRAVITY_PRIMARY_CHANNEL_NAME}"
        f"{fallback_line}"
    )


def _is_trusted_card_action(event: AstrMessageEvent) -> bool:
    msg = getattr(event, "message_obj", None)
    return (
        getattr(event, "is_card_action", False) is True
        or getattr(msg, "is_card_action", False) is True
    )


def _is_cli_provider(provider_id: str) -> bool:
    return provider_id.startswith(CLI_PROVIDER_PREFIX)


def _parse_cli_provider(provider_id: str) -> tuple[str, str, str | None]:
    model = provider_id.removeprefix(CLI_PROVIDER_PREFIX)
    if model.startswith("antigravity/"):
        return "antigravity", model.removeprefix("antigravity/"), None
    if model == "grok-build" or model.startswith("grok-build"):
        return "grok", model, None
    if model.startswith("codex/"):
        return "codex", model.removeprefix("codex/"), None
    if model.startswith("gemini-"):
        return "", model, None
    if model.startswith("claude-"):
        for effort in ("xhigh", "high", "medium", "low"):
            suffix = f"-{effort}"
            if model.endswith(suffix):
                return "claude", model.removesuffix(suffix), effort
        return "claude", model, "medium"
    return "", model, None


def _strip_known_prefix(text: str) -> str:
    stripped = text.lstrip()
    for prefix in ("#深度", "#PRD", "#prd", "#洞察", "#创意", "#舆情", "#代码"):
        if stripped.startswith(prefix):
            return stripped.removeprefix(prefix).strip() or stripped
    return stripped


# 非交互模式硬约束 — 所有 CLI 调用都要 prepend 这段
# 2026-05-20 修订（v2）：
# v1 的"让 Claude 自己假设"被用户否决——会编出"看起来专业但每个数字都假"的方案，
# 误导员工。改成"信息不足时明确反馈"，让员工补全后再发一次。
NON_INTERACTIVE_GUARD = (
    "【非交互模式硬约束 - 必读必守】\n"
    "- 你现在通过 CLI 单轮执行，无法向用户提问澄清。\n"
    "- 禁止调用 AskUserQuestion 或任何向用户发问的工具。\n"
    "- ⭐ 同样禁止编造或假设关键事实（品牌名、产品、预算、用户数据等）；\n"
    "  这家公司是真实的车企品牌营销服务公司，输出会直接给员工 / 老板看，\n"
    "  编出来的数字和场景一对照就穿，比拒绝服务还差。\n"
    "- ⭐ 公司客户触达默认不是邮件。客户部/市场/私域场景应默认输出微信私域、微信群、朋友圈、飞书消息、短信或话术草稿；\n"
    "  只有用户明确说“邮件/邮箱/email”时才输出邮件主题或邮件正文。\n"
    "  不要反复问“是否需要审查端午节问候邮件”这类与公司习惯不符的问题。\n"
    "\n"
    "处理规则（按这个顺序判断）:\n"
    "1. 用户消息里给了完成任务所需的关键事实 → 直接出方案。\n"
    "2. 关键事实严重不足（如没给品牌/产品/目标）→ 不要假装做。\n"
    "   输出格式应是『📋 信息补全请求』:\n"
    "      - 列出本次任务需要哪些字段（如品牌/产品/阶段/预算/受众/目标 KPI/周期）\n"
    "      - 每个字段说明为什么需要 + 给一个示范填法\n"
    "      - 结尾告诉员工：『请补全后再发一次给我』\n"
    "   字数控制在 200-400 字，让员工 1 分钟看完。\n"
    "3. 关键事实部分缺失 → 用 2-3 个典型档位的并列方案给出，\n"
    "   每档**明确标注**它假设的场景/预算/受众等，让员工选最匹配的一档。\n"
    "   严禁混在一起当作单一方案。\n"
    "\n"
    "总原则：真实优先。宁可输出一份『请你补这些信息』，也不要输出一份编造的方案。\n\n"
)


def _build_cli_prompt(event: AstrMessageEvent, decision) -> str:
    text = _strip_known_prefix(event.message_str or "")
    if decision.intent == "casual":
        return (
            "你是巅池-Agent小助手，负责飞书里的日常闲聊和轻量协助。\n"
            "请用自然、简短、亲切的中文回复，不要提到模型名，不要解释过程。\n\n"
            f"用户消息：{text}"
        )
    if decision.intent == "work_preflight":
        return (
            "你是巅池-Agent小助手，负责工作前置判断、需求澄清和轻量文案草稿。\n"
            "请先判断用户是要澄清问题、整理思路、改一句短文案，还是应该转入正式工作流。\n"
            "能直接给短草稿就直接给；信息不足时用 2-4 个问题补齐；不要硬做复杂终稿。\n"
            "中文回复，简短、具体、可执行，不要提到模型名。\n\n"
            f"用户消息：{text}"
        )
    if decision.intent == "realtime":
        return (
            "你是巅池-Agent小助手，负责轻量实时信息判断和热点问题快速回应。\n"
            "请优先给当前可判断的结论；如果需要外部事实但当前无法确认，请明确说明需要核实，"
            "不要编造具体时间、价格、排名或新闻细节。中文回复，简短、具体，不要提到模型名。\n\n"
            f"用户消息：{text}"
        )
    if decision.intent == "fallback":
        return (
            "你是巅池-Agent小助手，负责处理不明确但不是垃圾闲聊的短消息。\n"
            "请用中文给一个简短、有帮助的回应；如果用户意图不清，先帮他整理可能的方向，"
            "必要时只问 1-2 个澄清问题。不要提到模型名。\n\n"
            f"用户消息：{text}"
        )
    if decision.intent == "public_opinion":
        return (
            NON_INTERACTIVE_GUARD
            + "你是资深舆情管理、危机公关和热点攻防顾问。请用中文完成用户任务。\n"
            "要求：先判断舆情风险级别和核心矛盾，再给出回应策略、话术框架、"
            "渠道动作、监测指标和禁区提醒；如果用户只问一个轻量问题，直接简洁回答。\n"
            "不要提到模型名，不要解释执行过程。\n\n"
            f"用户任务：{text}"
        )
    if decision.intent in {"deep_insight", "deep_creative"}:
        return (
            NON_INTERACTIVE_GUARD
            + "你是资深品牌战略、营销策略与用户洞察顾问。请用中文完成用户任务。\n"
            "要求：结论先行，结构清晰，避免空话；给出可执行建议；"
            "如果是战略分析，请覆盖目标人群、传播主题、渠道打法、风险点和落地节奏。\n"
            "只输出最终内容，不要解释你的执行过程。\n\n"
            f"用户任务：{text}"
        )
    if decision.intent == "creative":
        return (
            NON_INTERACTIVE_GUARD
            + "你是资深营销创意总监。请用中文完成用户的创意任务。\n"
            "要求：直接给可用成稿；如果是 slogan/脚本/campaign，请给多个方向，"
            "每个方向说明核心洞察、文案和适用场景；避免空泛形容词。\n\n"
            f"用户任务：{text}"
        )
    if decision.intent == "insight":
        return (
            NON_INTERACTIVE_GUARD
            + "你是品牌战略与用户洞察顾问。请用中文完成用户任务。\n"
            "要求：结论先行，拆出目标人群、行为动机、关键张力、品牌机会和可执行建议；"
            "保持前台输出简洁但有判断。\n\n"
            f"用户任务：{text}"
        )
    return text


def _task_label(intent: str) -> str:
    return {
        "creative": "创意任务",
        "insight": "洞察任务",
        "deep_creative": "深度创意任务",
        "deep_insight": "深度任务",
    }.get(intent, "任务")


def _get_provider_by_id(context, provider_id: str):
    getter = getattr(context, "get_provider_by_id", None)
    if not callable(getter):
        return None
    try:
        return getter(provider_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[dc-router] get_provider_by_id(%s) 失败: %s", provider_id, exc)
        return None


def _get_provider_settings(context, event: AstrMessageEvent) -> dict:
    getter = getattr(context, "get_config", None)
    if not callable(getter):
        return {}
    try:
        cfg = getter(umo=event.unified_msg_origin)
    except TypeError:
        try:
            cfg = getter()
        except Exception:  # noqa: BLE001
            return {}
    except Exception:  # noqa: BLE001
        return {}

    if not isinstance(cfg, dict):
        return {}
    provider_settings = cfg.get("provider_settings", {})
    return provider_settings if isinstance(provider_settings, dict) else {}


def _image_compress_args(
    provider_settings: dict[str, object],
) -> tuple[int, int]:
    raw_options = provider_settings.get("image_compress_options", {})
    options = raw_options if isinstance(raw_options, dict) else {}

    max_size = options.get("max_size", IMAGE_COMPRESS_DEFAULT_MAX_SIZE)
    if not isinstance(max_size, int):
        max_size = IMAGE_COMPRESS_DEFAULT_MAX_SIZE
    max_size = max(max_size, 1)

    quality = options.get("quality", IMAGE_COMPRESS_DEFAULT_QUALITY)
    if not isinstance(quality, int):
        quality = IMAGE_COMPRESS_DEFAULT_QUALITY
    quality = min(max(quality, 1), 100)
    return max_size, quality


def _track_temporary_file(event: AstrMessageEvent, path: str | None) -> None:
    if not path:
        return
    tracker = getattr(event, "track_temporary_local_file", None)
    if callable(tracker):
        try:
            tracker(path)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[dc-router] track temp file 失败: %s", exc)


async def _prepare_image_ref(
    event: AstrMessageEvent,
    image: Image,
    provider_settings: dict[str, object],
) -> str | None:
    raw_ref = str(getattr(image, "url", "") or getattr(image, "file", "") or "")
    try:
        image_ref = await image.convert_to_file_path()
        if raw_ref.startswith(("http://", "https://", "base64://")):
            _track_temporary_file(event, image_ref)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[dc-router] 图片附件转本地路径失败，尝试使用原始引用: %s", exc)
        image_ref = raw_ref

    if not image_ref:
        return None

    try:
        max_size, quality = _image_compress_args(provider_settings)
        compressed = await compress_image(image_ref, max_size=max_size, quality=quality)
        if compressed and compressed != image_ref and os.path.exists(compressed):
            _track_temporary_file(event, compressed)
        return compressed or image_ref
    except Exception as exc:  # noqa: BLE001
        logger.warning("[dc-router] 图片压缩失败，使用原图: %s", exc)
        return image_ref


async def _summarize_file_attachment(file_comp: File) -> str:
    try:
        file_path = await file_comp.get_file()
    except Exception as exc:  # noqa: BLE001
        return (
            f"[File Attachment: name {file_comp.name or 'unknown'}, read failed: {exc}]"
        )

    file_name = file_comp.name or os.path.basename(file_path) or "unknown"
    path = Path(file_path)
    if not path.exists():
        return f"[File Attachment: name {file_name}, path {file_path}]"

    size = path.stat().st_size
    summary = (
        f"[File Attachment: name {file_name}, path {file_path}, size {size} bytes]"
    )
    text_suffixes = {
        ".txt",
        ".md",
        ".csv",
        ".json",
        ".yaml",
        ".yml",
        ".log",
        ".py",
        ".js",
        ".ts",
        ".html",
        ".css",
    }
    if path.suffix.lower() not in text_suffixes:
        return summary
    try:
        excerpt = path.read_text(encoding="utf-8", errors="replace")[:6000]
    except Exception as exc:  # noqa: BLE001
        return f"{summary}\n[File text read failed: {exc}]"
    return f"{summary}\n<File Excerpt>\n{excerpt}\n</File Excerpt>"


async def _collect_multimodal_inputs(
    event: AstrMessageEvent,
    provider_settings: dict[str, object],
) -> tuple[list[str], list[str], list[str], list[str]]:
    image_refs: list[str] = []
    audio_refs: list[str] = []
    video_refs: list[str] = []
    text_parts: list[str] = []

    try:
        message = event.message_obj.message
    except Exception as exc:  # noqa: BLE001
        logger.debug("[dc-router] 读取附件列表失败: %s", exc)
        return image_refs, audio_refs, video_refs, text_parts

    for comp in message:
        if isinstance(comp, Image):
            image_ref = await _prepare_image_ref(event, comp, provider_settings)
            if image_ref:
                image_refs.append(image_ref)
        elif isinstance(comp, Record):
            try:
                audio_path = await comp.convert_to_file_path()
                audio_refs.append(audio_path)
            except Exception as exc:  # noqa: BLE001
                text_parts.append(f"[Audio Attachment: read failed: {exc}]")
        elif isinstance(comp, File):
            text_parts.append(await _summarize_file_attachment(comp))
        elif isinstance(comp, Video):
            try:
                video_path = await comp.convert_to_file_path()
                video_name = os.path.basename(video_path)
                video_refs.append(video_path)
                text_parts.append(
                    f"[Video Attachment: name {video_name}, path {video_path}]"
                )
            except Exception as exc:  # noqa: BLE001
                text_parts.append(f"[Video Attachment: read failed: {exc}]")

    return image_refs, audio_refs, video_refs, text_parts


async def _preprocess_multimodal_attachments(
    context,
    event: AstrMessageEvent,
) -> str | None:
    """Caption/OCR attachments with AIHubMix Gemini Flash."""
    provider_settings = _get_provider_settings(context, event)
    image_refs, audio_refs, video_refs, text_parts = await _collect_multimodal_inputs(
        event,
        provider_settings,
    )
    if not image_refs and not audio_refs and not video_refs and not text_parts:
        return None

    provider_prompt = MULTIMODAL_PREPROCESS_PROMPT
    if text_parts:
        joined_text_parts = "\n\n".join(text_parts)[:8000]
        provider_prompt = f"{provider_prompt}\n\n补充附件信息：\n{joined_text_parts}"

    caption = ""
    provider_id = MULTIMODAL_PREPROCESS_PROVIDER_ID
    provider = _get_provider_by_id(context, provider_id)
    if provider is None:
        provider_id = MULTIMODAL_PREPROCESS_FALLBACK_PROVIDER_ID
        provider = _get_provider_by_id(context, provider_id)
    if provider is None:
        logger.warning(
            "[dc-router] 多模态 provider %s 不存在，fallback 到 v1.0",
            MULTIMODAL_PREPROCESS_FALLBACK_PROVIDER_ID,
        )
        return None

    if image_refs or audio_refs or text_parts:
        try:
            resp = await asyncio.wait_for(
                provider.text_chat(
                    prompt=provider_prompt,
                    image_urls=image_refs,
                    audio_urls=audio_refs,
                    contexts=[],
                ),
                timeout=60,
            )
            caption = (getattr(resp, "completion_text", "") or "").strip()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[dc-router] 多模态预处理失败: %s", exc)
            if image_refs or audio_refs:
                return None

    parts: list[str] = []
    if caption:
        parts.append(caption)
    elif text_parts:
        parts.append("\n".join(text_parts))
    summary = "\n\n".join(part for part in parts if part.strip()).strip()
    if not summary:
        return None

    event.set_extra("dc_router_multimodal_provider", provider_id)
    event.set_extra("dc_router_attachment_summary", summary)
    logger.info(
        "[dc-router] multimodal preprocessed provider=%s images=%s audio=%s video=%s text_parts=%s",
        provider_id,
        len(image_refs),
        len(audio_refs),
        len(video_refs),
        len(text_parts),
    )
    return summary


def _merge_attachment_summary_into_event(
    event: AstrMessageEvent,
    summary: str,
) -> None:
    base_text = (event.message_str or "").strip()
    merged = (
        f"{base_text}\n\n<attachment_summary>\n{summary}\n</attachment_summary>"
        if base_text
        else f"<attachment_summary>\n{summary}\n</attachment_summary>"
    )
    event.message_str = merged
    try:
        event.message_obj.message_str = merged
    except Exception:  # noqa: BLE001
        pass

    try:
        message = event.message_obj.message
        if isinstance(message, list):
            kept = [
                comp
                for comp in message
                if not isinstance(comp, Image | Record | File | Video)
            ]
            kept.append(
                Plain(f"<attachment_summary>\n{summary}\n</attachment_summary>")
            )
            event.message_obj.message = kept
    except Exception as exc:  # noqa: BLE001
        logger.debug("[dc-router] 回写附件摘要到 message chain 失败: %s", exc)


def _replace_event_text(event: AstrMessageEvent, text: str) -> None:
    event.message_str = text
    try:
        event.message_obj.message_str = text
    except Exception:  # noqa: BLE001
        pass


class AstrBotRouterClassifier:
    """Router LLM classifier backed by aihubmix Gemini 3.1 Pro provider."""

    def __init__(self, context) -> None:
        self.context = context

    async def classify(self, text: str):
        from dc_router.classifier import (
            ROUTER_CLASSIFIER_PROVIDER_ID,
            ROUTER_CLASSIFIER_SYSTEM_PROMPT,
            ClassifierResult,
        )
        from dc_router.taxonomy import RouterIntent

        provider = _get_provider_by_id(self.context, ROUTER_CLASSIFIER_PROVIDER_ID)
        if provider is None:
            logger.debug(
                "[dc-router] classifier provider %s 不存在，跳过 LLM 辅助裁判",
                ROUTER_CLASSIFIER_PROVIDER_ID,
            )
            return None

        try:
            resp = await asyncio.wait_for(
                provider.text_chat(
                    prompt=text[:2000],
                    system_prompt=ROUTER_CLASSIFIER_SYSTEM_PROMPT,
                    contexts=[],
                ),
                timeout=15,
            )
            raw = (getattr(resp, "completion_text", "") or "").strip()
            match = _CLASSIFIER_JSON_RE.search(raw)
            if not match:
                logger.debug("[dc-router] classifier 输出无 JSON: %r", raw[:120])
                return None
            data = json.loads(match.group(0))
            intent = RouterIntent(str(data.get("intent", "")).strip())
            confidence = float(data.get("confidence", 0.5) or 0.5)
            reason = str(data.get("reason", "classifier match")).strip()
            return ClassifierResult(
                intent=intent,
                confidence=max(0.0, min(confidence, 1.0)),
                reason=reason,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[dc-router] classifier 调用失败，跳过: %s", exc)
            return None


async def _direct_cli_answer(_context, event: AstrMessageEvent, decision) -> bool:
    """Handle DIRECT routes whose provider is a local CLI provider."""
    backend, model, _effort = _parse_cli_provider(decision.provider_id)
    if backend == "antigravity":
        gate = None
        job_id = ""
        try:
            from antigravity_health import (
                antigravity_allowed,
                mark_antigravity_failure,
                mark_antigravity_success,
                record_antigravity_circuit_fallback,
            )
            from cli_runner import CliRunner
            from dc_quota_runtime import get_quota_gate
            from harness import AdmissionMode, QuotaRequest

            allowed, health_reason, health_state = antigravity_allowed()
            if not allowed:
                record_antigravity_circuit_fallback(
                    reason=health_reason, state=health_state
                )
                available = {p.meta().id for p in _context.get_all_providers()}
                if ANTIGRAVITY_FALLBACK_PROVIDER_ID in available:
                    await _context.provider_manager.set_provider(
                        provider_id=ANTIGRAVITY_FALLBACK_PROVIDER_ID,
                        provider_type=ProviderType.CHAT_COMPLETION,
                        umo=event.unified_msg_origin,
                    )
                    event.set_extra(
                        "dc_router_antigravity_health_fallback",
                        {
                            "provider_id": decision.provider_id,
                            "fallback_provider_id": ANTIGRAVITY_FALLBACK_PROVIDER_ID,
                            "reason": health_reason,
                            "remaining_seconds": health_state.get(
                                "remaining_seconds", 0
                            ),
                        },
                    )
                    logger.info(
                        "[dc-router] Antigravity circuit open reason=%s remaining=%ss, "
                        "fallback=%s",
                        health_reason,
                        health_state.get("remaining_seconds", 0),
                        ANTIGRAVITY_FALLBACK_PROVIDER_ID,
                    )
                    return True
                return False

            prompt = await _inline_feishu_docs_in_text(
                _build_cli_prompt(event, decision)
            )
            original_prompt = (event.message_str or "").strip()
            platform_id = event.get_platform_id() or ""
            gate = await get_quota_gate()
            admission = await gate.admit(
                QuotaRequest(
                    primary_resource_key=ANTIGRAVITY_RESOURCE_KEY,
                    resource_keys=(ANTIGRAVITY_RESOURCE_KEY,),
                    payload={
                        "provider_id": decision.provider_id,
                        "backend": backend,
                        "intent": decision.intent,
                        "model": model,
                        "umo": event.unified_msg_origin,
                        "prompt": prompt,
                        "original_prompt": original_prompt,
                        "prompt_preview": prompt[:300],
                        "platform_id": platform_id,
                    },
                    requested_by=event.get_sender_id() or None,
                    session_id=event.unified_msg_origin,
                )
            )

            mode_value = (
                admission.mode.value
                if hasattr(admission.mode, "value")
                else str(admission.mode)
            )
            if mode_value == AdmissionMode.QUEUED.value:
                queue_position = admission.queue_position or 1
                event.should_call_llm(False)
                eta_text = _format_wait_minutes_from_eta(admission.eta_at)
                queue_card_message_id = await _start_antigravity_queue_card(
                    _context,
                    event,
                    gate,
                    job_id=admission.job.job_id,
                    queue_position=queue_position,
                    eta_at=admission.eta_at,
                    original_prompt=original_prompt,
                )
                event.set_extra(
                    "dc_router_antigravity_queue",
                    {
                        "provider_id": decision.provider_id,
                        "fallback_provider_id": ANTIGRAVITY_FALLBACK_PROVIDER_ID,
                        "primary_channel_name": ANTIGRAVITY_PRIMARY_CHANNEL_NAME,
                        "fallback_channel_name": ANTIGRAVITY_FALLBACK_CHANNEL_NAME,
                        "job_id": admission.job.job_id,
                        "queue_position": queue_position,
                        "eta_at": admission.eta_at,
                        "eta_text": eta_text,
                        "queue_card_message_id": queue_card_message_id,
                        "original_prompt": original_prompt,
                    },
                )
                event.set_result(
                    MessageEventResult()
                    .message(
                        "已进入小助手排队卡片，您也可以点按钮使用池池小助手先处理。"
                        if queue_card_message_id
                        else _antigravity_queue_message(
                            queue_position=queue_position,
                            eta_at=admission.eta_at,
                        )
                    )
                    .use_t2i(False)
                    .stop_event()
                )
                logger.info(
                    "[dc-router] Antigravity queued provider=%s pos=%s eta=%s "
                    "job=%s fallback_choice=%s",
                    decision.provider_id,
                    queue_position,
                    admission.eta_at,
                    admission.job.job_id,
                    ANTIGRAVITY_FALLBACK_PROVIDER_ID,
                )
                asyncio.create_task(
                    _watch_queued_antigravity_job(
                        context=_context,
                        gate=gate,
                        job_id=admission.job.job_id,
                        eta_at=admission.eta_at,
                        platform_id=platform_id,
                        stream_message_id=queue_card_message_id,
                    )
                )
                return True
            else:
                job_id = admission.job.job_id

            result = await CliRunner(cwd=_DC_AGENT_ROOT).run_antigravity(
                prompt,
                model=model,
                timeout=90,
            )
        except Exception as exc:  # noqa: BLE001
            try:
                from antigravity_health import mark_antigravity_failure

                mark_antigravity_failure(error_code="exception", error=str(exc))
            except Exception:  # noqa: BLE001
                pass
            if gate is not None and job_id:
                try:
                    await gate.fail(
                        job_id,
                        f"Antigravity CLI exception: {exc}",
                        retry_after_seconds=ANTIGRAVITY_COOLDOWN_SECONDS,
                    )
                except Exception:  # noqa: BLE001
                    pass
            logger.warning(
                "[dc-router] Antigravity CLI provider=%s 调用异常，fallback 到 v1.0: %s",
                decision.provider_id,
                exc,
            )
            return False

        if not result.ok:
            mark_antigravity_failure(error_code=result.error_code, error=result.error)
            if gate is not None and job_id:
                await gate.fail(
                    job_id,
                    f"{result.error_code or 'cli_error'}: {result.error or ''}",
                    retry_after_seconds=ANTIGRAVITY_COOLDOWN_SECONDS,
                )
            logger.info(
                "[dc-router] Antigravity CLI provider=%s 未接管 code=%s err=%s，fallback 到 v1.0",
                decision.provider_id,
                result.error_code,
                result.error,
            )
            available = {p.meta().id for p in _context.get_all_providers()}
            if ANTIGRAVITY_FALLBACK_PROVIDER_ID in available:
                await _context.provider_manager.set_provider(
                    provider_id=ANTIGRAVITY_FALLBACK_PROVIDER_ID,
                    provider_type=ProviderType.CHAT_COMPLETION,
                    umo=event.unified_msg_origin,
                )
                event.set_extra(
                    "dc_router_antigravity_failure_fallback",
                    {
                        "provider_id": decision.provider_id,
                        "fallback_provider_id": ANTIGRAVITY_FALLBACK_PROVIDER_ID,
                        "error_code": result.error_code,
                    },
                )
                return True
            return False

        mark_antigravity_success(elapsed_sec=result.elapsed_sec)
        if gate is not None and job_id:
            await gate.complete(
                job_id,
                result={
                    "provider_id": decision.provider_id,
                    "elapsed_sec": result.elapsed_sec,
                },
                cooldown_seconds=ANTIGRAVITY_COOLDOWN_SECONDS,
            )
        event.set_extra("dc_router_cli_provider", decision.provider_id)
        event.should_call_llm(False)
        event.set_result(
            MessageEventResult().message(result.text).use_t2i(False).stop_event()
        )
        logger.info(
            "[dc-router] DIRECT Antigravity CLI completed provider=%s model=%s elapsed=%.2fs",
            decision.provider_id,
            model,
            result.elapsed_sec,
        )
        return True

    if backend == "grok":
        prompt = (event.message_str or "").strip()
        try:
            from grok_worker import get_grok_worker

            prompt = await _inline_feishu_docs_in_text(
                _build_cli_prompt(event, decision)
            )
            result = await get_grok_worker().ask_public_opinion(prompt)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[dc-router] Grok Build provider=%s 调用异常，尝试 fallback=%s: %s",
                decision.provider_id,
                GROK_BUILD_FALLBACK_PROVIDER_ID,
                exc,
            )
            result = None

        if result is None or not result.ok:
            error_code = (
                result.error_code if result is not None else "exception"
            ) or "unknown"
            error = result.error if result is not None else "Grok Build exception"
            available = {p.meta().id for p in _context.get_all_providers()}
            if GROK_BUILD_FALLBACK_PROVIDER_ID in available:
                await _context.provider_manager.set_provider(
                    provider_id=GROK_BUILD_FALLBACK_PROVIDER_ID,
                    provider_type=ProviderType.CHAT_COMPLETION,
                    umo=event.unified_msg_origin,
                )
                _replace_event_text(event, prompt)
                event.set_extra(
                    "dc_router_grok_build_fallback",
                    {
                        "provider_id": decision.provider_id,
                        "fallback_provider_id": GROK_BUILD_FALLBACK_PROVIDER_ID,
                        "error_code": error_code,
                        "error": str(error or "")[:300],
                    },
                )
                logger.info(
                    "[dc-router] Grok Build provider=%s 未接管 code=%s，fallback=%s",
                    decision.provider_id,
                    error_code,
                    GROK_BUILD_FALLBACK_PROVIDER_ID,
                )
                return True
            logger.warning(
                "[dc-router] Grok Build provider=%s 失败且 fallback provider 不存在 code=%s err=%s",
                decision.provider_id,
                error_code,
                error,
            )
            return False

        event.set_extra("dc_router_cli_provider", decision.provider_id)
        event.set_extra("dc_router_grok_build_elapsed_sec", result.elapsed_sec)
        event.should_call_llm(False)
        event.set_result(
            MessageEventResult().message(result.text).use_t2i(False).stop_event()
        )
        logger.info(
            "[dc-router] DIRECT Grok Build completed provider=%s model=%s elapsed=%.2fs",
            decision.provider_id,
            model,
            result.elapsed_sec,
        )
        return True

    if backend != "codex":
        logger.warning(
            "[dc-router] DIRECT cli provider %s 暂未接管，fallback 到 v1.0",
            decision.provider_id,
        )
        return False

    task_label = _task_label(decision.intent)
    tier = event.get_extra("reasoning_tier")
    if not tier:
        if "xhigh" in decision.provider_id:
            tier = "xhigh"
        elif "high" in decision.provider_id:
            tier = "high"
        elif "medium" in decision.provider_id:
            tier = "medium"
        else:
            tier = "minimal"
    card_info = await _start_progress_card(
        _context,
        event,
        task_label,
        tier,
        current_stage="Codex CLI 正在处理",
        interval_sec=10.0,
    )
    streamer = card_info[0] if card_info else None
    stream_message_id = card_info[1] if card_info else None

    try:
        from cli_runner import CliRunner

        prompt = await _inline_feishu_docs_in_text(_build_cli_prompt(event, decision))
        result = await CliRunner(cwd=_DC_AGENT_ROOT).run_codex(
            prompt,
            model=model,
            timeout=300,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[dc-router] Codex CLI provider=%s 调用异常，fallback 到 v1.0: %s",
            decision.provider_id,
            exc,
        )
        if streamer and stream_message_id:
            await _finalize_error_card(
                streamer, stream_message_id, task_label, str(exc)
            )
            event.should_call_llm(False)
            event.set_result(
                MessageEventResult().message("").use_t2i(False).stop_event()
            )
            return True
        return False

    if not result.ok:
        logger.warning(
            "[dc-router] Codex CLI provider=%s 失败 code=%s err=%s，fallback 到 v1.0",
            decision.provider_id,
            result.error_code,
            result.error,
        )
        if streamer and stream_message_id:
            await _finalize_error_card(
                streamer,
                stream_message_id,
                task_label,
                result.error or result.error_code or "unknown cli error",
            )
            event.should_call_llm(False)
            event.set_result(
                MessageEventResult().message("").use_t2i(False).stop_event()
            )
            return True
        return False

    event.set_extra("dc_router_cli_provider", decision.provider_id)
    event.set_extra("reasoning_tier", tier)
    event.should_call_llm(False)
    if streamer and stream_message_id:
        ok = await _finalize_response_card(streamer, stream_message_id, result.text)
        if ok:
            event.set_result(
                MessageEventResult().message("").use_t2i(False).stop_event()
            )
        else:
            event.set_result(
                MessageEventResult().message(result.text).use_t2i(False).stop_event()
            )
    else:
        event.set_result(
            MessageEventResult().message(result.text).use_t2i(False).stop_event()
        )
    logger.info(
        "[dc-router] DIRECT Codex CLI completed provider=%s model=%s elapsed=%.2fs",
        decision.provider_id,
        model,
        result.elapsed_sec,
    )
    return True


def _format_eta_minutes(eta_at: float | None) -> str:
    if not eta_at:
        return "预计稍后开始。"
    minutes = max(1, math.ceil((eta_at - time.time()) / 60))
    return f"预计约 {minutes} 分钟后开始。"


async def _send_context_message(context, umo: str, text: str) -> bool:
    try:
        return bool(await context.send_message(umo, MessageChain([Plain(text)])))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[dc-router] 后台回传飞书失败 umo=%s: %s", umo, exc)
        return False


def _fallback_cli_failure_report(
    *,
    task_label: str,
    provider_id: str,
    model: str,
    effort: str | None,
    error: str,
) -> str:
    effort_text = f" / effort={effort}" if effort else ""
    return (
        f"⚠️ {task_label}执行异常，已自动触发故障处理。\n\n"
        f"- 故障链路：{provider_id} ({model}{effort_text})\n"
        f"- 初步原因：{error[:500]}\n"
        "- 已处理：该任务已从 QuotaGate 标记失败，避免继续占用深度任务资源；"
        "如是超时/僵死，本地 CLI 进程组会被强制结束。\n"
        "- 建议：可以直接重发任务；如果连续出现同类问题，优先检查 Claude CLI 登录态、"
        "网络代理、订阅额度和本机残留进程。"
    )


async def _codex_cli_failure_report(
    *,
    provider_id: str,
    model: str,
    effort: str | None,
    task_label: str,
    prompt: str,
    error: str,
) -> str:
    """Use Codex CLI to produce a short operational report for failed Claude jobs."""
    base_report = _fallback_cli_failure_report(
        task_label=task_label,
        provider_id=provider_id,
        model=model,
        effort=effort,
        error=error,
    )
    try:
        from cli_runner import CliRunner

        diag_prompt = (
            "你是 DC-Agent 的运维诊断助手。Claude CLI 深度任务刚刚失败或疑似僵死。\n"
            "请用中文给员工/小助手会话一条简洁、可执行的自动处理回复。\n"
            "要求：\n"
            "1. 先说明任务没有丢，系统已自动处理故障。\n"
            "2. 判断最可能原因（超时、退出码、登录态、额度、网络、进程残留等）。\n"
            "3. 给 2-4 条下一步建议。\n"
            "4. 如果原任务信息足够，给一个轻量临时方案骨架；不要假装已经完成完整深度报告。\n"
            "5. 控制在 250-500 字。\n\n"
            f"任务类型：{task_label}\n"
            f"失败 provider：{provider_id}\n"
            f"模型：{model}\n"
            f"effort：{effort or '-'}\n"
            f"错误：{error[:2000]}\n\n"
            f"原始任务 prompt：\n{prompt[:5000]}"
        )
        result = await CliRunner(cwd=_DC_AGENT_ROOT).run_codex(
            diag_prompt,
            model=CLI_FAILURE_DIAG_MODEL,
            timeout=CLI_FAILURE_DIAG_TIMEOUT_SECONDS,
        )
        if result.ok:
            return (
                "⚠️ 深度任务执行异常，已由 Codex CLI 自动诊断：\n\n"
                f"{result.text.strip()}"
            )
        logger.warning(
            "[dc-router] Codex CLI failure diagnosis failed code=%s err=%s",
            result.error_code,
            result.error,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[dc-router] Codex CLI failure diagnosis exception: %s", exc)
    return base_report


# ─────────────────── 飞书卡片渲染 (2026-05-20 加入) ───────────────────
# Claude CLI 输出经 streamer 渲染成卡片，跟 v1 daily_card_renderer
# 一致的字号体系（heading_0/1/2/3 + normal）+ 自动颜色判定。


def _extract_chat_info(event: AstrMessageEvent) -> tuple[str, str]:
    """从 lark event 提取 chat_id + receive_id_type（参考 hermes_escalation_plugin）。"""
    raw_msg = getattr(event.message_obj, "raw_message", None)
    chat_id = getattr(raw_msg, "chat_id", None) or ""
    if not chat_id:
        chat_id = event.get_group_id() or event.get_sender_id() or ""
    receive_id_type = "chat_id" if chat_id.startswith("oc_") else "open_id"
    return chat_id, receive_id_type


def _header_color_from_text(text: str) -> str:
    """前 200 字含 ⚠️/风险 → orange；含 ✅/完成 → green；其他 blue。
    保留 daily_card_renderer 一致的颜色判定逻辑（用户硬要求保留）。"""
    first200 = (text or "")[:200]
    if any(kw in first200 for kw in ("⚠️", "风险", "警告", "失败", "错误")):
        return "orange"
    if any(kw in first200 for kw in ("✅", "完成", "成功", "通过")):
        return "green"
    return "blue"


def _extract_title_from_markdown(text: str) -> tuple[str | None, str]:
    """从 markdown 第一行 # 标题提到 header；返回 (title, 剩余 markdown)。

    跟 daily_card_renderer 的行为对齐：让卡片有 header 彩条 + 大标题，
    但 markdown 正文不重复出现第一行。
    """
    if not text:
        return None, text
    lines = text.lstrip().split("\n", 1)
    first = lines[0].strip()
    rest = lines[1] if len(lines) > 1 else ""
    if first.startswith("#") and len(first) <= 80:
        # 去掉 # 前缀
        title = first.lstrip("#").strip()
        if title:
            return title, rest.strip()
    return None, text


async def _start_progress_card(
    context,
    event: AstrMessageEvent,
    task_label: str,
    reasoning_tier: str | None,
    *,
    current_stage: str | None = None,
    queue_position: int | None = None,
    eta_text: str | None = None,
    interval_sec: float = 15.0,
) -> tuple[object, str, str, str] | None:
    """派发任务时立刻起一张进度卡 + 启动 15 秒自动刷新。

    Returns:
        (streamer, message_id, chat_id, receive_id_type) 或 None（streamer 不可用）
    """
    try:
        platform_id = event.get_platform_id() or ""
        from dc_engines.feishu_card_streamer import start_waiting_card_for_event

        brief = (event.message_str or "").strip()[:200] or task_label
        tier = reasoning_tier or "medium"
        handle = await start_waiting_card_for_event(
            context,
            event,
            title=task_label,
            brief=brief,
            reasoning_tier=tier,
            current_stage=current_stage,
            queue_position=queue_position,
            eta_text=eta_text,
            interval_sec=interval_sec,
        )
        if handle is None:
            logger.debug(
                "[dc-router] platform=%r 无 streamer，跳过卡片（fallback 到纯文本）",
                platform_id,
            )
            return None

        logger.info(
            "[dc-router] 进度卡已发 platform=%s message_id=%s task=%s tier=%s",
            platform_id,
            handle.message_id,
            task_label,
            tier,
        )
        return (
            handle.streamer,
            handle.message_id,
            handle.chat_id,
            handle.receive_id_type,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[dc-router] 起进度卡失败: %s", exc)
        return None


async def _antigravity_queue_snapshot(
    gate,
    job_id: str,
    *,
    fallback_position: int,
    fallback_eta_at: float | None,
) -> tuple[int, str]:
    try:
        job = await gate.store.get_job(job_id)
        pending_jobs = await gate.list_pending_jobs(limit=200)
        if job is None:
            return fallback_position, _format_wait_minutes_from_eta(fallback_eta_at)
        same_queue = [
            item
            for item in pending_jobs
            if item.primary_resource_key == job.primary_resource_key
            and item.priority >= job.priority
        ]
        for index, item in enumerate(same_queue, start=1):
            if item.job_id == job_id:
                return index, _format_wait_minutes_from_eta(job.eta_at)
        return fallback_position, _format_wait_minutes_from_eta(job.eta_at)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[dc-router] 读取 agy 队列快照失败: %s", exc)
        return fallback_position, _format_wait_minutes_from_eta(fallback_eta_at)


async def _start_antigravity_queue_card(
    context,
    event: AstrMessageEvent,
    gate,
    *,
    job_id: str,
    queue_position: int,
    eta_at: float | None,
    original_prompt: str,
    interval_sec: float = 12.0,
) -> str | None:
    try:
        from dc_engines.card_runtime import send_card_via_runtime
        from dc_engines.feishu_card_streamer import (
            build_antigravity_queue_card,
            ensure_streamers_on_context,
        )

        platform_id = event.get_platform_id() or ""
        streamer = ensure_streamers_on_context(context).get(platform_id)
        if streamer is None:
            return None
        chat_id, receive_id_type = _extract_chat_info(event)
        if not chat_id:
            return None
        eta_text = _format_wait_minutes_from_eta(eta_at)
        card = build_antigravity_queue_card(
            job_id=job_id,
            queue_position=queue_position,
            eta_text=eta_text,
            primary_channel_name=ANTIGRAVITY_PRIMARY_CHANNEL_NAME,
            fallback_channel_name=ANTIGRAVITY_FALLBACK_CHANNEL_NAME,
            fallback_provider_id=ANTIGRAVITY_FALLBACK_PROVIDER_ID,
            original_prompt=original_prompt,
        )
        stream = await send_card_via_runtime(
            streamer,
            card_type="thinking_waiting",
            chat_id=chat_id,
            receive_id_type=receive_id_type,
            card=card,
            platform_id=platform_id,
            event="start",
            detail="antigravity queue card",
        )
        if stream is None:
            return None

        queue_cards = getattr(context, "dc_antigravity_queue_cards", None)
        if queue_cards is None:
            queue_cards = {}
            context.dc_antigravity_queue_cards = queue_cards
        queue_cards[job_id] = {
            "streamer": streamer,
            "message_id": stream.message_id,
            "queue_position": queue_position,
            "eta_at": eta_at,
            "original_prompt": original_prompt,
        }

        async def _builder(s):
            latest_position, latest_eta_text = await _antigravity_queue_snapshot(
                gate,
                job_id,
                fallback_position=queue_position,
                fallback_eta_at=eta_at,
            )
            return build_antigravity_queue_card(
                job_id=job_id,
                queue_position=latest_position,
                eta_text=latest_eta_text,
                elapsed_sec=s.elapsed_sec,
                primary_channel_name=ANTIGRAVITY_PRIMARY_CHANNEL_NAME,
                fallback_channel_name=ANTIGRAVITY_FALLBACK_CHANNEL_NAME,
                fallback_provider_id=ANTIGRAVITY_FALLBACK_PROVIDER_ID,
                original_prompt=original_prompt,
            )

        streamer.start_auto_update(
            stream.message_id, _builder, interval_sec=interval_sec
        )
        logger.info(
            "[dc-router] Antigravity 排队卡已发 job=%s message_id=%s pos=%s",
            job_id,
            stream.message_id,
            queue_position,
        )
        return stream.message_id
    except Exception as exc:  # noqa: BLE001
        logger.warning("[dc-router] Antigravity 排队卡创建失败: %s", exc)
        return None


async def _send_plain_hint(event: AstrMessageEvent, text: str) -> None:
    try:
        result = event.send(MessageChain([Plain(text)]))
        if asyncio.iscoroutine(result):
            await result
    except Exception as exc:  # noqa: BLE001
        logger.debug("[dc-router] 发送提示失败: %s", exc)


async def _update_antigravity_queue_card_to_fallback(
    context,
    *,
    job_id: str,
    queue_position: int = 1,
    eta_text: str = "稍后",
    original_prompt: str = "",
) -> None:
    queue_cards = getattr(context, "dc_antigravity_queue_cards", {}) or {}
    info = queue_cards.get(job_id)
    if not info:
        return
    try:
        from dc_engines.card_runtime import finalize_card_via_runtime
        from dc_engines.feishu_card_streamer import build_antigravity_queue_card

        streamer = info.get("streamer")
        message_id = info.get("message_id", "")
        if not streamer or not message_id:
            return
        card = build_antigravity_queue_card(
            job_id=job_id,
            queue_position=int(info.get("queue_position") or queue_position),
            eta_text=eta_text,
            primary_channel_name=ANTIGRAVITY_PRIMARY_CHANNEL_NAME,
            fallback_channel_name=ANTIGRAVITY_FALLBACK_CHANNEL_NAME,
            fallback_provider_id=ANTIGRAVITY_FALLBACK_PROVIDER_ID,
            original_prompt=original_prompt or str(info.get("original_prompt") or ""),
            status="fallback_running",
        )
        await finalize_card_via_runtime(
            streamer,
            card_type="thinking_waiting",
            message_id=message_id,
            card=card,
            platform_id="",
            detail="antigravity queue fallback finalized",
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[dc-router] 更新 Antigravity 排队卡为 fallback 状态失败: %s", exc)


async def maybe_handle_antigravity_queue_card_action(
    context,
    event: AstrMessageEvent,
) -> bool:
    text = event.message_str or ""
    if not text.startswith("__card_action__:"):
        return False
    if not _is_trusted_card_action(event):
        logger.warning(
            "[dc-router] 拒绝非可信 Antigravity 排队卡动作 sender=%s",
            str(event.get_sender_id() or "")[:12],
        )
        event.should_call_llm(False)
        event.set_result(MessageEventResult().message("").use_t2i(False).stop_event())
        return True
    try:
        payload = json.loads(text[len("__card_action__:") :])
    except Exception as exc:  # noqa: BLE001
        logger.warning("[dc-router] 排队卡 payload 解析失败: %s", exc)
        event.should_call_llm(False)
        event.set_result(MessageEventResult().message("").use_t2i(False).stop_event())
        return True
    value = payload.get("value", {}) or {}
    if value.get("source") != "antigravity_queue_card":
        return False
    if value.get("action") != "use_fallback":
        event.should_call_llm(False)
        event.set_result(MessageEventResult().message("").use_t2i(False).stop_event())
        return True

    job_id = str(value.get("job_id") or "").strip()
    fallback_provider_id = str(
        value.get("fallback_provider_id") or ANTIGRAVITY_FALLBACK_PROVIDER_ID
    ).strip()
    original_prompt = str(value.get("original_prompt") or "").strip()
    queue_cards = getattr(context, "dc_antigravity_queue_cards", {}) or {}
    if not original_prompt and job_id in queue_cards:
        original_prompt = str(queue_cards[job_id].get("original_prompt") or "")

    available = {p.meta().id for p in context.get_all_providers()}
    if fallback_provider_id not in available:
        event.should_call_llm(False)
        event.set_result(
            MessageEventResult()
            .message("池池小助手现在也有点忙，我先继续帮您保留排队位置。")
            .use_t2i(False)
            .stop_event()
        )
        return True

    try:
        from dc_quota_runtime import get_quota_gate

        gate = await get_quota_gate()
        cancelled = await gate.cancel_pending_job(
            job_id,
            reason=f"User selected {ANTIGRAVITY_FALLBACK_CHANNEL_NAME}.",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[dc-router] 取消 Antigravity 队列失败: %s", exc)
        cancelled = False

    if not cancelled:
        event.should_call_llm(False)
        event.set_result(
            MessageEventResult()
            .message("池池小助手现在也有点忙，我先继续帮您保留排队位置。")
            .use_t2i(False)
            .stop_event()
        )
        return True

    await context.provider_manager.set_provider(
        provider_id=fallback_provider_id,
        provider_type=ProviderType.CHAT_COMPLETION,
        umo=event.unified_msg_origin,
    )
    _replace_event_text(event, original_prompt)
    event.set_extra("dc_router_antigravity_fallback_job_id", job_id)
    event.set_extra(
        "dc_router_antigravity_fallback_channel", ANTIGRAVITY_FALLBACK_CHANNEL_NAME
    )
    await _update_antigravity_queue_card_to_fallback(
        context,
        job_id=job_id,
        original_prompt=original_prompt,
    )
    await _send_plain_hint(event, "好的，我马上请池池小助手先帮您处理～")
    return True


async def _finalize_response_card(
    streamer,
    message_id: str,
    result_text: str,
) -> bool:
    """CLI 完成时把进度卡 finalize 成完整响应卡（复用 build_daily_response_card）。

    样式：heading_0/1/2/3 字号体系 + normal 正文 14px + header 自动颜色判定。
    """
    try:
        from dc_engines.card_runtime import finalize_card_via_runtime
        from dc_engines.feishu_card_streamer import build_daily_response_card

        title, content_md = _extract_title_from_markdown(result_text)
        header_color = _header_color_from_text(result_text)
        final_card = build_daily_response_card(
            content_md=content_md,
            title=title,
            header_color=header_color,
            footer_hint="由 dc-router CLI 生成",
        )
        await finalize_card_via_runtime(
            streamer,
            card_type="daily_response",
            message_id=message_id,
            card=final_card,
            platform_id="",
            detail="dc-router cli response finalized",
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[dc-router] finalize 响应卡失败 message_id=%s: %s", message_id, exc
        )
        return False


async def _finalize_error_card(
    streamer,
    message_id: str,
    task_label: str,
    error: str,
) -> bool:
    """CLI 失败时把进度卡 finalize 成错误卡。"""
    try:
        from dc_engines.card_runtime import finalize_card_via_runtime
        from dc_engines.feishu_card_streamer import build_error_card

        error_card = build_error_card(
            title=f"⚠️ {task_label}执行失败",
            error_msg=str(error)[:500],
        )
        await finalize_card_via_runtime(
            streamer,
            card_type="daily_response",
            message_id=message_id,
            card=error_card,
            platform_id="",
            detail="dc-router cli error finalized",
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[dc-router] finalize 错误卡失败 message_id=%s: %s", message_id, exc
        )
        return False


# ─────────── 飞书 doc fetch helpers ───────────


def _load_feishu_creds_for_router() -> tuple[str, str] | None:
    """读 feishu_whitelist.yaml 取 lark bot 凭证（跟 feishu_doc_fetcher 同源）。"""
    try:
        if not _FEISHU_CREDS_PATH.exists():
            return None
        import yaml

        data = yaml.safe_load(_FEISHU_CREDS_PATH.read_text(encoding="utf-8")) or {}
        feishu = data.get("feishu", {}) if isinstance(data, dict) else {}
        if not isinstance(feishu, dict) or not feishu.get("enable", True):
            return None
        app_id = (feishu.get("app_id") or "").strip()
        app_secret = (feishu.get("app_secret") or "").strip()
        if not app_id or not app_secret:
            return None
        return app_id, app_secret
    except Exception as exc:  # noqa: BLE001
        logger.debug("[dc-router] 读飞书凭证失败: %s", exc)
        return None


async def _fetch_feishu_doc_inline(url: str) -> tuple[bool, str, str]:
    """读一个 feishu wiki/docx URL 的正文。

    Returns:
        (ok, content_text, error_msg)
    """
    m = _FEISHU_URL_RE.search(url)
    if not m:
        return False, "", "not a feishu URL"
    kind = m.group(1)
    token = m.group(2)

    creds = _load_feishu_creds_for_router()
    if not creds:
        return False, "", "feishu 凭证未配（feishu_whitelist.yaml）"
    app_id, app_secret = creds

    try:
        import lark_oapi as lark
        from lark_oapi.api.docx.v1 import RawContentDocumentRequest
        from lark_oapi.api.wiki.v2 import GetNodeSpaceRequest

        client = (
            lark.Client.builder()
            .app_id(app_id)
            .app_secret(app_secret)
            .log_level(lark.LogLevel.WARNING)
            .build()
        )

        if kind == "wiki":
            req = GetNodeSpaceRequest.builder().token(token).build()
            resp = await client.wiki.v2.space.aget_node(req)
            if resp.code != 0 or not resp.data or not resp.data.node:
                return False, "", f"wiki API code={resp.code} msg={resp.msg}"
            doc_token = resp.data.node.obj_token
            obj_type = resp.data.node.obj_type
            if obj_type != "docx":
                return (
                    False,
                    "",
                    f"unsupported wiki obj_type={obj_type}（暂只支持 docx）",
                )
        else:
            doc_token = token

        req2 = RawContentDocumentRequest.builder().document_id(doc_token).build()
        resp2 = await client.docx.v1.document.araw_content(req2)
        if resp2.code != 0 or not resp2.data:
            return False, "", f"docx API code={resp2.code} msg={resp2.msg}"
        return True, resp2.data.content or "", ""
    except Exception as exc:  # noqa: BLE001
        return False, "", f"{type(exc).__name__}: {exc}"


async def _inline_feishu_docs_in_text(text: str) -> str:
    """检测文本里所有飞书 URL，替换为 <feishu_doc>...</feishu_doc> 块。

    抓取失败时也保留占位（标记 error=true），让 LLM 知道用户引用了一个 URL 但拿不到。
    """
    if not text:
        return text
    matches = list(_FEISHU_URL_RE.finditer(text))
    if not matches:
        return text

    parts: list[str] = []
    last_end = 0
    for m in matches:
        parts.append(text[last_end : m.start()])
        url = m.group(0)
        ok, content, error = await _fetch_feishu_doc_inline(url)
        if ok:
            truncated = content[:_FEISHU_DOC_MAX_CHARS]
            note = (
                f"\n[原文共 {len(content)} 字符，已截断到 {_FEISHU_DOC_MAX_CHARS}]"
                if len(content) > _FEISHU_DOC_MAX_CHARS
                else ""
            )
            parts.append(
                f'\n\n<feishu_doc url="{url}">\n{truncated}{note}\n</feishu_doc>\n\n'
            )
            logger.info(
                "[dc-router] inline 飞书文档 url=%s len=%d", url[:80], len(content)
            )
        else:
            parts.append(
                f'\n\n<feishu_doc url="{url}" error="true">\n'
                f"抓取失败：{error}\n（请告诉员工把内容粘贴过来，或者检查 bot 是否有该 wiki 权限）\n"
                f"</feishu_doc>\n\n"
            )
            logger.warning(
                "[dc-router] inline 飞书文档失败 url=%s err=%s", url[:80], error
            )
        last_end = m.end()
    parts.append(text[last_end:])
    return "".join(parts)


async def _run_harness_cli_job(
    *,
    context,
    gate,
    job_id: str,
    umo: str,
    provider_id: str,
    backend: str,
    model: str,
    effort: str | None,
    prompt: str,
    task_label: str = "任务",
    platform_id: str = "",
    stream_message_id: str | None = None,
) -> None:
    """跑 CLI + 写 QuotaGate 状态 + 把进度卡 finalize 成响应卡/错误卡。

    platform_id + stream_message_id 同时存在时启用卡片渲染，
    否则 fallback 到纯文本（_send_context_message）。这个签名用基本类型，
    方便 QuotaGate payload 序列化 + _watch/_resume 透传。
    """
    streamer = None
    if platform_id and stream_message_id:
        try:
            from dc_engines.feishu_card_streamer import ensure_streamers_on_context

            streamers = ensure_streamers_on_context(context)
            streamer = streamers.get(platform_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[dc-router] 拿 streamer 失败 platform=%s: %s", platform_id, exc
            )

    has_card = streamer is not None and stream_message_id

    async def _report_failure(msg: str, *, report_text: str | None = None) -> None:
        text = report_text or f"{task_label}执行失败：{msg}"
        if has_card:
            await _finalize_error_card(streamer, stream_message_id, task_label, text)
        else:
            await _send_context_message(context, umo, text)

    try:
        from cli_runner import CliRunner

        runner = CliRunner()
        if backend == "claude":
            result = await runner.run_claude(
                prompt,
                model=model,
                effort=effort or "medium",
                timeout=900,
            )
        else:
            msg = f"Unsupported CLI backend: {backend}"
            await gate.fail(job_id, msg, retry_after_seconds=60)
            await _report_failure(msg)
            return

        if not result.ok:
            error = result.error or result.error_code or "unknown cli error"
            await gate.fail(job_id, error, retry_after_seconds=60)
            report_text = await _codex_cli_failure_report(
                provider_id=provider_id,
                model=model,
                effort=effort,
                task_label=task_label,
                prompt=prompt,
                error=error,
            )
            await _report_failure(error, report_text=report_text)
            return

        await gate.complete(
            job_id,
            result={
                "provider_id": provider_id,
                "model": model,
                "effort": effort,
                "elapsed_sec": result.elapsed_sec,
                "model_usage": result.model_usage,
                "task_label": task_label,
            },
        )
        if has_card:
            ok = await _finalize_response_card(streamer, stream_message_id, result.text)
            if not ok:
                # 卡片 finalize 失败 → fallback 纯文本，至少员工能拿到结果
                await _send_context_message(context, umo, result.text)
        else:
            await _send_context_message(context, umo, result.text)
        logger.info(
            "[dc-router] CLI job completed provider=%s elapsed=%.2fs job=%s card=%s",
            provider_id,
            result.elapsed_sec,
            job_id,
            bool(has_card),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[dc-router] HERMES CLI job failed job=%s: %s", job_id, exc)
        try:
            await gate.fail(job_id, str(exc), retry_after_seconds=60)
        except Exception as fail_exc:  # noqa: BLE001
            logger.warning(
                "[dc-router] QuotaGate.fail 失败 job=%s: %s", job_id, fail_exc
            )
        report_text = await _codex_cli_failure_report(
            provider_id=provider_id,
            model=model,
            effort=effort,
            task_label=task_label,
            prompt=prompt,
            error=str(exc),
        )
        await _report_failure(str(exc), report_text=report_text)


async def _run_antigravity_cli_job(
    *,
    context,
    gate,
    job_id: str,
    umo: str,
    provider_id: str,
    model: str,
    prompt: str,
    platform_id: str = "",
    stream_message_id: str | None = None,
) -> None:
    """Run a queued Antigravity job and finalize its waiting card."""
    streamer = None
    if platform_id and stream_message_id:
        try:
            from dc_engines.feishu_card_streamer import ensure_streamers_on_context

            streamers = ensure_streamers_on_context(context)
            streamer = streamers.get(platform_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[dc-router] 拿 Antigravity streamer 失败 platform=%s: %s",
                platform_id,
                exc,
            )

    has_card = streamer is not None and stream_message_id

    async def _report_failure(msg: str) -> None:
        text = f"小助手执行失败：{msg}"
        if has_card:
            await _finalize_error_card(streamer, stream_message_id, "小助手", text)
        else:
            await _send_context_message(context, umo, text)

    try:
        from antigravity_health import (
            mark_antigravity_failure,
            mark_antigravity_success,
        )
        from cli_runner import CliRunner

        result = await CliRunner(cwd=_DC_AGENT_ROOT).run_antigravity(
            prompt,
            model=model,
            timeout=90,
        )
        if not result.ok:
            error = result.error or result.error_code or "unknown antigravity error"
            mark_antigravity_failure(error_code=result.error_code, error=error)
            await gate.fail(
                job_id,
                error,
                retry_after_seconds=ANTIGRAVITY_COOLDOWN_SECONDS,
            )
            await _report_failure(error)
            return

        mark_antigravity_success(elapsed_sec=result.elapsed_sec)
        await gate.complete(
            job_id,
            result={
                "provider_id": provider_id,
                "model": model,
                "elapsed_sec": result.elapsed_sec,
            },
            cooldown_seconds=ANTIGRAVITY_COOLDOWN_SECONDS,
        )
        if has_card:
            ok = await _finalize_response_card(streamer, stream_message_id, result.text)
            if not ok:
                await _send_context_message(context, umo, result.text)
        else:
            await _send_context_message(context, umo, result.text)
        logger.info(
            "[dc-router] queued Antigravity job completed provider=%s elapsed=%.2fs job=%s card=%s",
            provider_id,
            result.elapsed_sec,
            job_id,
            bool(has_card),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[dc-router] queued Antigravity job failed job=%s: %s", job_id, exc
        )
        try:
            await gate.fail(
                job_id,
                str(exc),
                retry_after_seconds=ANTIGRAVITY_COOLDOWN_SECONDS,
            )
        except Exception as fail_exc:  # noqa: BLE001
            logger.warning(
                "[dc-router] QuotaGate.fail 失败 job=%s: %s", job_id, fail_exc
            )
        await _report_failure(str(exc))


async def _watch_queued_antigravity_job(
    *,
    context,
    gate,
    job_id: str,
    eta_at: float | None,
    platform_id: str = "",
    stream_message_id: str | None = None,
) -> None:
    delay = max(1, int((eta_at or time.time()) - time.time()))
    await asyncio.sleep(delay)

    for _ in range(10):
        job = await gate.start_pending_job(job_id)
        if job is not None:
            payload = job.payload
            umo = str(payload.get("umo") or job.session_id or "")
            if not stream_message_id:
                await _send_context_message(
                    context,
                    umo,
                    "排队中的小助手任务开始执行了，完成后我会把结果发回这里。",
                )
            await _run_antigravity_cli_job(
                context=context,
                gate=gate,
                job_id=job.job_id,
                umo=umo,
                provider_id=str(payload.get("provider_id") or ANTIGRAVITY_PROVIDER_ID),
                model=str(payload.get("model") or "gemini-3.5-flash"),
                prompt=str(
                    payload.get("prompt") or payload.get("original_prompt") or ""
                ),
                platform_id=platform_id or str(payload.get("platform_id") or ""),
                stream_message_id=stream_message_id,
            )
            return
        await asyncio.sleep(60)

    logger.warning(
        "[dc-router] queued Antigravity job did not become runnable: %s", job_id
    )


async def _watch_queued_harness_cli_job(
    *,
    context,
    gate,
    job_id: str,
    eta_at: float | None,
    platform_id: str = "",
    stream_message_id: str | None = None,
) -> None:
    delay = max(1, int((eta_at or time.time()) - time.time()))
    await asyncio.sleep(delay)

    for _ in range(10):
        job = await gate.start_pending_job(job_id)
        if job is not None:
            payload = job.payload
            umo = str(payload.get("umo") or job.session_id or "")
            if stream_message_id and platform_id:
                try:
                    from dc_engines.feishu_card_streamer import (
                        build_progress_card,
                        ensure_streamers_on_context,
                    )

                    streamers = ensure_streamers_on_context(context)
                    streamer = streamers.get(platform_id)
                    if streamer is not None:
                        stream = streamer.get_stream(stream_message_id)
                        if stream and stream.auto_update_task:
                            stream.auto_update_task.cancel()
                            try:
                                await stream.auto_update_task
                            except (asyncio.CancelledError, Exception):
                                pass
                            stream.auto_update_task = None
                        task_label = str(payload.get("task_label") or "任务")
                        brief = str(payload.get("prompt_preview") or task_label)[:200]
                        tier = payload.get("reasoning_tier") or "medium"

                        def _builder(s):
                            return build_progress_card(
                                title=task_label,
                                brief=brief,
                                elapsed_sec=s.elapsed_sec,
                                reasoning_tier=tier,
                                current_stage="资源已就绪，正在执行",
                            )

                        if stream:
                            await streamer.update(
                                stream_message_id,
                                _builder(stream),
                            )
                        streamer.start_auto_update(
                            stream_message_id,
                            _builder,
                            interval_sec=15.0,
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.debug("[dc-router] queued card retarget failed: %s", exc)
            if not stream_message_id:
                await _send_context_message(
                    context,
                    umo,
                    f"排队中的{payload.get('task_label') or '任务'}开始执行了，完成后我会把结果发回这里。",
                )
            await _run_harness_cli_job(
                context=context,
                gate=gate,
                job_id=job.job_id,
                umo=umo,
                provider_id=str(payload.get("provider_id") or ""),
                backend=str(payload.get("backend") or ""),
                model=str(payload.get("model") or ""),
                effort=payload.get("effort"),
                prompt=str(payload.get("prompt") or ""),
                task_label=str(payload.get("task_label") or "任务"),
                platform_id=platform_id or str(payload.get("platform_id") or ""),
                stream_message_id=stream_message_id or payload.get("stream_message_id"),
            )
            return
        await asyncio.sleep(60)

    logger.warning("[dc-router] queued CLI job did not become runnable: %s", job_id)


async def _resume_pending_harness_cli_jobs(context, gate, *, limit: int = 20) -> int:
    resumed = 0
    pending_jobs = await gate.list_pending_jobs(limit=limit)
    for pending in pending_jobs:
        payload = pending.payload
        provider_id = str(payload.get("provider_id") or "")
        if not provider_id.startswith("cli/"):
            continue
        backend = str(payload.get("backend") or "")
        if backend not in {"claude", "antigravity"}:
            continue

        job = await gate.start_pending_job(pending.job_id)
        if job is None:
            continue

        resumed += 1
        payload = job.payload
        umo = str(payload.get("umo") or job.session_id or "")
        if backend == "antigravity":
            await _send_context_message(
                context,
                umo,
                "排队中的小助手任务恢复执行了，完成后我会把结果发回这里。",
            )
            asyncio.create_task(
                _run_antigravity_cli_job(
                    context=context,
                    gate=gate,
                    job_id=job.job_id,
                    umo=umo,
                    provider_id=str(
                        payload.get("provider_id") or ANTIGRAVITY_PROVIDER_ID
                    ),
                    model=str(payload.get("model") or "gemini-3.5-flash"),
                    prompt=str(
                        payload.get("prompt") or payload.get("original_prompt") or ""
                    ),
                    platform_id=str(payload.get("platform_id") or ""),
                    stream_message_id=payload.get("stream_message_id"),
                )
            )
        else:
            await _send_context_message(
                context=context,
                umo=umo,
                text=f"排队中的{payload.get('task_label') or '任务'}恢复执行了，完成后我会把结果发回这里。",
            )
            asyncio.create_task(
                _run_harness_cli_job(
                    context=context,
                    gate=gate,
                    job_id=job.job_id,
                    umo=umo,
                    provider_id=str(payload.get("provider_id") or ""),
                    backend=backend,
                    model=str(payload.get("model") or ""),
                    effort=payload.get("effort"),
                    prompt=str(payload.get("prompt") or ""),
                    task_label=str(payload.get("task_label") or "任务"),
                    platform_id=str(payload.get("platform_id") or ""),
                    stream_message_id=payload.get("stream_message_id"),
                )
            )
    return resumed


async def _queue_recovery_loop(context, interval_seconds: int) -> None:
    from dc_quota_runtime import get_quota_gate

    gate = await get_quota_gate()
    while True:
        try:
            resumed = await _resume_pending_harness_cli_jobs(context, gate)
            if resumed:
                logger.info("[dc-router] recovered %s pending CLI jobs", resumed)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("[dc-router] pending queue recovery failed: %s", exc)
        await asyncio.sleep(interval_seconds)


def start_queue_recovery(context, *, interval_seconds: int = 60) -> None:
    """Start one background scanner for persisted pending queued CLI jobs."""
    global _QUEUE_RECOVERY_TASK
    if _QUEUE_RECOVERY_TASK is not None and not _QUEUE_RECOVERY_TASK.done():
        return
    _QUEUE_RECOVERY_TASK = asyncio.create_task(
        _queue_recovery_loop(context, interval_seconds)
    )
    logger.info(
        "[dc-router] pending queue recovery started · interval=%ss",
        interval_seconds,
    )


def stop_queue_recovery() -> None:
    """Stop the background pending queue scanner."""
    global _QUEUE_RECOVERY_TASK
    if _QUEUE_RECOVERY_TASK is not None and not _QUEUE_RECOVERY_TASK.done():
        _QUEUE_RECOVERY_TASK.cancel()
    _QUEUE_RECOVERY_TASK = None


async def _enqueue_or_run_harness_cli(
    context, event: AstrMessageEvent, decision
) -> bool:
    """Admit a queued FRONT/HERMES CLI task through QuotaGate."""
    if not _is_cli_provider(decision.provider_id):
        return False

    backend, model, effort = _parse_cli_provider(decision.provider_id)
    if backend != "claude":
        logger.warning(
            "[dc-router] queued cli provider %s 暂未接入，fallback 到 v1.0",
            decision.provider_id,
        )
        return False
    if not decision.resource_keys:
        logger.warning(
            "[dc-router] queued cli provider %s 缺 resource_keys，fallback 到 v1.0",
            decision.provider_id,
        )
        return False

    try:
        from dc_quota_runtime import get_quota_gate
        from harness import AdmissionMode, QuotaRequest

        gate = await get_quota_gate()
        # 抓取消息里引用的飞书文档 → 内联到 prompt（让 Claude CLI 拿到完整原文）
        prompt = await _inline_feishu_docs_in_text(_build_cli_prompt(event, decision))
        task_label = _task_label(decision.intent)
        platform_id = event.get_platform_id() or ""
        reasoning_tier = event.get_extra("reasoning_tier") or "medium"

        admission = await gate.admit(
            QuotaRequest(
                primary_resource_key=decision.resource_keys[0],
                resource_keys=tuple(decision.resource_keys),
                payload={
                    "provider_id": decision.provider_id,
                    "backend": backend,
                    "intent": decision.intent,
                    "model": model,
                    "effort": effort,
                    "umo": event.unified_msg_origin,
                    "prompt": prompt,
                    "task_label": task_label,
                    "prompt_preview": prompt[:300],
                    # 卡片渲染相关 (持久化到 QuotaGate payload, _watch/_resume 能复用)
                    "platform_id": platform_id,
                    "stream_message_id": None,
                    "reasoning_tier": reasoning_tier,
                },
                requested_by=event.get_sender_id() or None,
                session_id=event.unified_msg_origin,
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[dc-router] QuotaGate admit 失败: %s · fallback 到 v1.0", exc)
        return False

    fallback_provider_id = CLI_QUEUE_OAUTH_FALLBACK_PROVIDER_ID
    mode_value = (
        admission.mode.value
        if hasattr(admission.mode, "value")
        else str(admission.mode)
    )

    if mode_value == AdmissionMode.QUEUED.value:
        available = {p.meta().id for p in context.get_all_providers()}
        if fallback_provider_id in available:
            try:
                cancelled = await gate.cancel_pending_job(
                    admission.job.job_id,
                    reason=(
                        "CLI queued; routed to Codex OAuth fallback provider "
                        f"{fallback_provider_id}"
                    ),
                )
                if not cancelled:
                    logger.warning(
                        "[dc-router] queued job=%s cancel failed, keep CLI queue path",
                        admission.job.job_id,
                    )
                else:
                    await context.provider_manager.set_provider(
                        provider_id=fallback_provider_id,
                        provider_type=ProviderType.CHAT_COMPLETION,
                        umo=event.unified_msg_origin,
                    )
                    _replace_event_text(event, prompt)
                    event.set_extra(
                        "dc_router_cli_queue_fallback", decision.provider_id
                    )
                    event.set_extra(
                        "dc_router_oauth_fallback_provider",
                        fallback_provider_id,
                    )
                    event.set_extra("reasoning_tier", "xhigh")
                    logger.info(
                        "[dc-router] CLI queued provider=%s job=%s → OAuth fallback=%s",
                        decision.provider_id,
                        admission.job.job_id,
                        fallback_provider_id,
                    )
                    return True
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[dc-router] OAuth fallback provider=%s 切换失败，保留 CLI 队列: %s",
                    fallback_provider_id,
                    exc,
                )

        event.should_call_llm(False)
        queue_position = admission.queue_position or 1
        eta_text = _format_eta_minutes(admission.eta_at)
        card_info = await _start_progress_card(
            context,
            event,
            task_label,
            reasoning_tier,
            current_stage="资源忙，已进入深度任务队列",
            queue_position=queue_position,
            eta_text=eta_text,
            interval_sec=10.0,
        )
        stream_message_id = card_info[1] if card_info else None
        event.set_result(
            MessageEventResult()
            .message(
                f"已进入{task_label}队列，等待卡会持续计时。"
                if stream_message_id
                else f"已进入{task_label}队列，前面还有 {queue_position} 个任务。{eta_text}"
            )
            .use_t2i(False)
            .stop_event()
        )
        logger.info(
            "[dc-router] queued CLI provider=%s pos=%s eta=%s job=%s",
            decision.provider_id,
            queue_position,
            admission.eta_at,
            admission.job.job_id,
        )
        asyncio.create_task(
            _watch_queued_harness_cli_job(
                context=context,
                gate=gate,
                job_id=admission.job.job_id,
                eta_at=admission.eta_at,
                platform_id=platform_id,
                stream_message_id=stream_message_id,
            )
        )
        return True

    if mode_value != AdmissionMode.RUN_NOW.value:
        logger.warning(
            "[dc-router] unknown AdmissionMode=%s · fallback 到 v1.0", admission.mode
        )
        return False

    event.should_call_llm(False)
    card_info = await _start_progress_card(context, event, task_label, reasoning_tier)
    stream_message_id = card_info[1] if card_info else None
    short_reply = (
        f"⏳ {task_label}处理中（看下方卡片实时进度）"
        if stream_message_id
        else f"已进入{task_label}执行，完成后我会把结果发回这里。"
    )
    event.set_result(
        MessageEventResult().message(short_reply).use_t2i(False).stop_event()
    )
    asyncio.create_task(
        _run_harness_cli_job(
            context=context,
            gate=gate,
            job_id=admission.job.job_id,
            umo=event.unified_msg_origin,
            provider_id=decision.provider_id,
            backend=backend,
            model=model,
            effort=effort,
            prompt=prompt,
            task_label=task_label,
            platform_id=platform_id,
            stream_message_id=stream_message_id,
        )
    )
    logger.info(
        "[dc-router] queued CLI admitted provider=%s model=%s effort=%s job=%s",
        decision.provider_id,
        model,
        effort,
        admission.job.job_id,
    )
    return True


def event_to_envelope(event: AstrMessageEvent):
    """AstrMessageEvent → dc_router.MessageEnvelope。

    出错时返回最小 envelope（只含 text），让 DCRouter 至少能跑关键词 + classifier。
    """
    from dc_router import AttachmentKind, MessageEnvelope

    attachment_kinds: list = []
    try:
        for comp in event.message_obj.message:
            ctype = type(comp).__name__.lower()
            if "image" in ctype:
                attachment_kinds.append(AttachmentKind.IMAGE)
            elif "record" in ctype or "voice" in ctype or "audio" in ctype:
                attachment_kinds.append(AttachmentKind.VOICE)
            elif "video" in ctype:
                attachment_kinds.append(AttachmentKind.VIDEO)
            elif "file" in ctype:
                attachment_kinds.append(AttachmentKind.FILE)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[dc-router-adapter] 附件类型检测异常（忽略）: %s", exc)

    metadata: dict[str, str] = {
        "platform_id": event.get_platform_id() or "",
    }

    return MessageEnvelope(
        text=event.message_str or "",
        attachment_kinds=tuple(attachment_kinds),
        user_id=event.get_sender_id() or None,
        session_id=event.unified_msg_origin or None,
        metadata=metadata,
    )


async def apply_decision(context, event: AstrMessageEvent, decision) -> bool:
    """RouterDecision → AstrBot 动作。

    Returns:
        True  - dc-router 已处理（plugin 应停止，不走 v1 兜底）
        False - dc-router 没接管（plugin 应 fallback 到 v1.0）
    """
    from dc_router import RouteDepth

    provider_id = decision.provider_id
    intent = decision.intent
    depth = decision.depth
    action = decision.action

    # depth 是 pydantic v2 use_enum_values 后会变成字符串
    depth_value = depth.value if hasattr(depth, "value") else str(depth)
    action_value = action.value if hasattr(action, "value") else str(action)

    if depth_value != RouteDepth.DIRECT.value:
        if depth_value in {RouteDepth.FRONT.value, RouteDepth.HERMES.value}:
            return await _enqueue_or_run_harness_cli(context, event, decision)

        logger.info(
            "[dc-router] depth=%s 暂未接入，intent=%s fallback 到 v1.0",
            depth_value,
            intent,
        )
        return False

    # DIRECT + cli/* path: local CLI answers and stops the event.
    if _is_cli_provider(provider_id):
        event.set_extra("llm_router_intent", intent)
        event.set_extra("llm_router_source", decision.source)
        event.set_extra("llm_router_provider", provider_id)
        return await _direct_cli_answer(context, event, decision)

    # DIRECT path: 切 provider 即可
    available = {p.meta().id for p in context.get_all_providers()}
    if provider_id not in available:
        logger.warning(
            "[dc-router] target provider %s 不在 available 列表，fallback 到 v1.0",
            provider_id,
        )
        return False

    try:
        await context.provider_manager.set_provider(
            provider_id=provider_id,
            provider_type=ProviderType.CHAT_COMPLETION,
            umo=event.unified_msg_origin,
        )

        # 把 reasoning_tier 透传给下游卡片（保持跟 v1.0 兼容）
        tier = None
        if "xhigh" in provider_id:
            tier = "xhigh"
        elif "high" in provider_id:
            tier = "high"
        elif "medium" in provider_id:
            tier = "medium"
        elif "5.4" in provider_id or "minimal" in provider_id:
            tier = "minimal"
        if tier:
            event.set_extra("reasoning_tier", tier)
        event.set_extra("llm_router_intent", intent)
        event.set_extra("llm_router_source", decision.source)
        event.set_extra("llm_router_provider", provider_id)

        text_preview = (event.message_str or "")[:30].replace("\n", " ")
        logger.info(
            "[dc-router] intent=%s · action=%s · provider=%s · tier=%s · '%s' · source=%s",
            intent,
            action_value,
            provider_id,
            tier or "-",
            text_preview,
            decision.source,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[dc-router] set_provider 失败 (%s): %s · fallback 到 v1.0",
            provider_id,
            exc,
        )
        return False


async def route_via_dc_router(
    context,
    event: AstrMessageEvent,
    dry_run: bool = True,
) -> bool:
    """plugin 的 enabled=true 入口。

    Args:
        dry_run: True (默认) = 只记录决策，不切 provider/不入队/不派 Hermes
                 False = 真按 RouterDecision 切 provider 等动作

    Returns:
        True  - dc-router 已处理（仅在 dry_run=False 且 apply_decision 成功时）
        False - 让 plugin 走 v1.0 兜底（dry_run=True 时永远返回 False）
    """
    try:
        from dc_router import DCRouter

        envelope = event_to_envelope(event)

        if _is_local_casual_ack(
            envelope.text.strip(), has_attachments=envelope.has_attachments
        ):
            if dry_run:
                logger.info(
                    "[dc-router · DRY-RUN] would handle local casual ack without provider"
                )
                return False
            event.should_call_llm(False)
            event.set_extra("llm_router_intent", "local_casual_ack")
            event.set_extra("llm_router_source", "local_rule")
            event.set_extra("llm_router_provider", "local/no_llm")
            event.set_result(
                MessageEventResult().message("").use_t2i(False).stop_event()
            )
            logger.info("[dc-router] local casual ack handled without provider")
            return True

        # 太短或空消息直接放弃（跟 v1.0 行为一致）
        if not envelope.text.strip() or len(envelope.text.strip()) < 2:
            if not envelope.has_attachments:
                return False  # 没文本没附件 → 让 v1 自己处理（v1 也会跳过）

        dc_router = DCRouter(classifier=AstrBotRouterClassifier(context))
        decision = await dc_router.decide(envelope)

        # ─── dry-run 分支：只 log 不动 ───
        if dry_run:
            text_preview = envelope.text[:30].replace("\n", " ")
            logger.info(
                "[dc-router · DRY-RUN] would route: intent=%s · depth=%s · "
                "provider=%s · platform=%s · source=%s · '%s' "
                "(返回 False 让 v1.0 实际处理)",
                decision.intent,
                decision.depth,
                decision.provider_id,
                envelope.metadata.get("platform_id", "?"),
                decision.source,
                text_preview,
            )
            return False  # 让 plugin 走 v1.0 实际响应

        if decision.needs_multimodal_preprocess:
            summary = await _preprocess_multimodal_attachments(context, event)
            if not summary:
                logger.warning("[dc-router] 多模态预处理无结果，fallback 到 v1.0")
                return False
            _merge_attachment_summary_into_event(event, summary)
            envelope = event_to_envelope(event)
            decision = await dc_router.decide(envelope)

        # 正式模式：真切 provider / 真派
        return await apply_decision(context, event, decision)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[dc-router-adapter] route_via_dc_router 异常，fallback 到 v1.0: %s",
            exc,
        )
        return False
