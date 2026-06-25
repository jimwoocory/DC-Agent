"""Render LLM replies as Feishu interactive cards.

The decoration hook runs after the LLM produces a result and before AstrBot
sends it, allowing this plugin to finalize a waiting card or render a fallback
card while consuming the original text result.

Only applies to Lark platforms with ``feishu_streamers`` mounted on context.
"""

from __future__ import annotations

import re

from dc_engines.card_runtime import (
    finalize_card_via_runtime,
    send_card_via_runtime,
)
from dc_engines.card_system import (
    run_card_system_health,
    should_render_casual_reply_card,
    should_start_waiting_card,
)
from dc_engines.feishu_card_streamer import (
    build_casual_response_card,
    build_daily_response_card,
    build_thinking_card,
    ensure_streamers_on_context,
)

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.core.message.message_event_result import ResultContentType

# Event key for the waiting-card stream id.
_STREAM_KEY = "_daily_card_thinking_stream_id"
_BRIEF_KEY = "_daily_card_user_brief"

# Waiting cards are reserved for task-like requests.
MIN_THINKING_PLACEHOLDER_CHARS = 1

# Minimum text length for structured-card fallback.
MIN_CARD_CHARS = 150

# Structured markdown markers.
_STRUCTURE_MARKERS = re.compile(
    r"^#{1,4} |\*\*[^*]+\*\*|^\|.+\|.+\||^- |^\d+\. |^> ",
    re.MULTILINE,
)

# Format preference appended to the system prompt.
_FORMAT_HINT = (
    "\n\n## 📋 回复格式偏好（飞书展示用）\n"
    "- 超过 80 字的回复，请用 markdown 结构化：用 `## 章节标题` 分块、"
    "关键点用 `**加粗**`、清单用列表（`- xxx`）、数据用表格（`| a | b |`）。\n"
    "- 短回复（80 字以内）保持自然口语，不强行加 markdown。\n"
    "- 重要结论可放在第一段开头，让用户一眼看见。\n"
    "- 多段内容时，每段之间用空行分隔（飞书卡片按空行切段渲染）。"
)


def _is_card_worthy(text: str) -> bool:
    """Return whether a reply should be rendered as a structured card."""
    if not text or len(text) < MIN_CARD_CHARS:
        return False
    return bool(_STRUCTURE_MARKERS.search(text))


def _strip_model_thinking(text: str) -> str:
    """Strip likely internal thinking or reasoning preambles."""
    if not text or len(text) < 80:
        return text

    lines = text.split("\n")
    if len(lines) < 3:
        return text

    first_line = lines[0].strip()
    # Detect likely thinking preambles.
    thinking_prefixes = (
        "i will",
        "i'm going to",
        "i am going to",
        "let me",
        "我来",
        "我将",
    )
    if not any(first_line.lower().startswith(p) for p in thinking_prefixes):
        return text

    # Find the boundary between thinking text and the user-facing reply.
    boundary = None
    for i in range(len(lines) - 1):
        curr = lines[i].rstrip()
        nxt = lines[i + 1].strip()

        if curr.endswith((".", "?")) and nxt:
            if "\u4e00" <= nxt[0] <= "\u9fff":
                boundary = i
                break
            if nxt[0].isupper() and not nxt.startswith("/") and not nxt.startswith("`"):
                boundary = i
                break

    if boundary is None:
        first_p = lines[0]
        has_file_path = bool(re.search(r"/[Uu]sers/|/data/|/DC-|/astrbot", first_p))
        ends_with_reason = bool(
            re.search(
                r"\bto (understand|see|check|verify|find|determine)\b",
                first_p,
                re.IGNORECASE,
            )
        )
        if has_file_path or ends_with_reason:
            return text
        return text

    remaining_lines = lines[boundary + 1 :]
    if len(remaining_lines) < 2:
        return text

    result_text = "\n".join(remaining_lines).strip()
    logger.debug(
        "[daily_card_renderer] stripped thinking block (%d lines → %d lines, %d → %d chars)",
        len(lines),
        len(remaining_lines),
        len(text),
        len(result_text),
    )
    return result_text


def _rebuild_streamer_from_event(event: AstrMessageEvent, context):
    """Rebuild a missing streamer from context.platform_manager as fallback."""
    from dc_engines.feishu_card_streamer.streamer import FeishuCardStreamer

    try:
        platform_manager = getattr(context, "platform_manager", None)
        if not platform_manager:
            return None
        platform_insts = getattr(platform_manager, "platform_insts", None) or []
        for inst in platform_insts:
            lark_api = getattr(inst, "lark_api", None)
            if lark_api is not None:
                platform_id = event.get_platform_id() or ""
                streamer = FeishuCardStreamer(lark_api)
                # Register it back onto context for later calls.
                streamers = getattr(context, "feishu_streamers", None) or {}
                if not isinstance(streamers, dict):
                    streamers = {}
                streamers[platform_id] = streamer
                context.feishu_streamers = streamers  # type: ignore[attr-defined]
                logger.info(
                    "[daily_card_renderer] 重建 streamer 成功 platform=%s",
                    platform_id,
                )
                return streamer
    except Exception:
        pass
    return None


def _extract_title(text: str) -> str | None:
    """Extract the first markdown heading as the card title."""
    for line in text.split("\n", 5):
        s = line.strip()
        if s.startswith("# "):
            return s.lstrip("# ").strip()[:50]
        if s.startswith("## "):
            return s.lstrip("# ").strip()[:50]
    return None


def _is_lark_event(event: AstrMessageEvent) -> bool:
    """Return True only for Feishu/Lark message events.

    The renderer sends Feishu interactive cards. Webchat/OpenAPI smoke events can
    carry synthetic sender ids such as ``ou_smoke_user_*``; treating those as
    Feishu ``open_id`` values creates noisy 99992351 failures and hides real
    card-delivery defects.
    """
    platform_name = ""
    try:
        platform_name = str(event.get_platform_name() or "").lower()
    except Exception:
        platform_name = ""
    if platform_name in {"lark", "feishu"}:
        return True
    platform_id = str(event.get_platform_id() or "").lower()
    return platform_id in {"lark", "feishu"} or "飞书" in platform_id


def _should_use_waiting_card(event: AstrMessageEvent) -> bool:
    intent = str(event.get_extra("dc_router_intent") or "").strip()
    return should_start_waiting_card(
        intent=intent,
        message=event.message_str or "",
        reasoning_tier=event.get_extra("reasoning_tier"),
    )


def _should_render_casual_card(event: AstrMessageEvent, text: str) -> bool:
    intent = str(event.get_extra("dc_router_intent") or "").strip()
    return should_render_casual_reply_card(
        intent=intent,
        message=event.message_str or "",
    )


def _consume_rendered_result(result) -> None:
    """Mark an already-rendered model result as consumed by card delivery."""
    result.chain.clear()
    result.set_result_content_type(ResultContentType.GENERAL_RESULT)


@register(
    "daily_card_renderer",
    "dc_agent",
    "LLM 长回复自动渲染成飞书 interactive card（级别 3）",
    "1.1.0",
)
class DailyCardRendererPlugin(Star):
    # Plugin-level dedup consumes duplicate results so AstrBot does not send
    # them as plain text. The streamer also dedupes duplicate patch calls.
    _FINALIZED_DEDUP_CAP = 1024

    def __init__(self, context: Context) -> None:
        super().__init__(context)
        self._log_card_system_health()
        # Track finalized stream ids so later re-entry consumes the result
        # without recording a false plain-text fallback.
        self._finalized_stream_ids: dict[str, None] = {}

    def _log_card_system_health(self) -> None:
        report = run_card_system_health()
        if report.ok:
            logger.info(
                "[card_system] health OK: %d checks, %d registered cards",
                len(report.checks),
                sum(1 for name in report.checks if name.startswith("sample:")),
            )
            return
        failed = [name for name, ok in report.checks.items() if not ok]
        logger.warning(
            "[card_system] health CHECK FAILED (non-blocking): %s details=%s",
            ", ".join(failed),
            report.details,
        )

    async def _start_thinking_card_if_needed(self, event: AstrMessageEvent) -> None:
        """Create one waiting card for a lark LLM request if it does not exist yet."""
        if not _is_lark_event(event):
            return
        if event.get_extra(_STREAM_KEY):
            return

        platform_id = event.get_platform_id() or ""
        streamers = ensure_streamers_on_context(self.context)
        streamer = streamers.get(platform_id)
        if streamer is None:
            return

        user_msg = (event.message_str or "").strip()
        if len(user_msg) < MIN_THINKING_PLACEHOLDER_CHARS:
            return
        if not _should_use_waiting_card(event):
            return

        raw_msg = getattr(event.message_obj, "raw_message", None)
        chat_id = getattr(raw_msg, "chat_id", None) or ""
        if not chat_id:
            chat_id = event.get_group_id() or event.get_sender_id() or ""
        if not chat_id:
            return
        receive_id_type = "chat_id" if chat_id.startswith("oc_") else "open_id"

        tier = event.get_extra("reasoning_tier")
        card = build_thinking_card(
            user_msg=user_msg,
            elapsed_sec=0,
            reasoning_tier=tier,
        )
        stream = await send_card_via_runtime(
            streamer,
            card_type="thinking_waiting",
            chat_id=chat_id,
            receive_id_type=receive_id_type,
            card=card,
            platform_id=platform_id,
            event="start",
            detail="daily renderer waiting card",
        )
        if stream is None:
            return

        event.set_extra(_STREAM_KEY, stream.message_id)
        event.set_extra(_BRIEF_KEY, user_msg)

        def _builder(s):
            return build_thinking_card(
                user_msg=user_msg,
                elapsed_sec=s.elapsed_sec,
                reasoning_tier=tier,
            )

        streamer.start_auto_update(stream.message_id, _builder, interval_sec=3.0)
        logger.info(
            "[daily_card_renderer] 等待卡已发 message_id=%s msg=%r",
            stream.message_id,
            user_msg[:40],
        )

    @filter.on_waiting_llm_request()
    async def send_waiting_card_before_llm_lock(
        self,
        event: AstrMessageEvent,
    ) -> None:
        """Send a waiting card before queue/lock waits when needed."""
        await self._start_thinking_card_if_needed(event)

    @filter.on_llm_request(priority=10)
    async def inject_format_preference_and_ensure_thinking_card(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> None:
        """Inject formatting preference and ensure the waiting card exists."""
        req.system_prompt = (req.system_prompt or "") + _FORMAT_HINT
        await self._start_thinking_card_if_needed(event)

    @filter.on_decorating_result(priority=30)
    async def finalize_or_render_card(
        self,
        event: AstrMessageEvent,
    ) -> None:
        """Finalize waiting cards or render a fallback card for the LLM result."""
        platform_id = event.get_platform_id() or ""
        if not _is_lark_event(event):
            logger.debug(
                "[daily_card_renderer] skip non-lark platform=%s name=%s",
                platform_id,
                getattr(event, "get_platform_name", lambda: "")(),
            )
            return
        if not platform_id:
            logger.warning("[daily_card_renderer] 无法获取 platform_id，跳过卡片渲染")
            return
        streamers = ensure_streamers_on_context(self.context)
        streamer = streamers.get(platform_id)
        if streamer is None:
            streamer = _rebuild_streamer_from_event(event, self.context)
            if streamer is None:
                logger.warning(
                    "[daily_card_renderer] streamer 缺失（platform=%s），跳过卡片渲染。文本将裸发。"
                    " 如此消息频繁出现请检查 platform_manager 是否正确加载飞书平台。",
                    platform_id,
                )
                return

        result = event.get_result()
        if not result or not result.chain:
            return
        if event.get_extra("dc_media_route_handled"):
            logger.debug(
                "[daily_card_renderer] skip card render for media route platform=%s",
                platform_id,
            )
            return

        # Extract Plain text.
        plain_parts: list[str] = []
        for comp in result.chain:
            if isinstance(comp, Plain):
                plain_parts.append(comp.text or "")
        full_text = "\n".join(plain_parts).strip()
        if not full_text:
            return

        # Strip internal thinking before deciding which card to render.
        full_text = _strip_model_thinking(full_text)

        # Prefer finalizing an existing waiting card.
        stream_id = event.get_extra(_STREAM_KEY)
        if stream_id:
            # Multi-turn LLM flows can re-enter this hook with the same stream.
            # Consume duplicate results instead of recording false fallbacks.
            if stream_id in self._finalized_stream_ids:
                logger.debug(
                    "[daily_card_renderer] 重复 finalize 跳过 (dedup hit) "
                    "stream_id=%s full_text_len=%d",
                    stream_id,
                    len(full_text),
                )
                _consume_rendered_result(result)
                return

            # Choose header color and title.
            title = _extract_title(full_text) or "巅池-Agent小助手"
            header_color = "blue"
            first200 = full_text[:200]
            if any(kw in first200 for kw in ("⚠️", "风险", "警告", "失败")):
                header_color = "orange"
            elif any(kw in first200 for kw in ("✅", "完成", "成功", "通过")):
                header_color = "green"

            final_card = build_daily_response_card(
                content_md=full_text,
                title=title,
                header_color=header_color,
            )
            ok = await finalize_card_via_runtime(
                streamer,
                card_type="daily_response",
                message_id=stream_id,
                card=final_card,
                platform_id=platform_id,
                detail="waiting card finalized",
            )
            if not ok:
                return
            # Remember finalized streams with FIFO eviction.
            if len(self._finalized_stream_ids) >= self._FINALIZED_DEDUP_CAP:
                oldest = next(iter(self._finalized_stream_ids))
                self._finalized_stream_ids.pop(oldest, None)
            self._finalized_stream_ids[stream_id] = None
            _consume_rendered_result(result)
            logger.info(
                "[daily_card_renderer] 占位卡 finalize message_id=%s len=%d",
                stream_id,
                len(full_text),
            )
            return

        async def _send_casual_card(detail: str) -> bool:
            raw_msg = getattr(event.message_obj, "raw_message", None)
            chat_id = getattr(raw_msg, "chat_id", None) or ""
            if not chat_id:
                chat_id = event.get_group_id() or event.get_sender_id() or ""
            if not chat_id:
                return False
            receive_id_type = "chat_id" if chat_id.startswith("oc_") else "open_id"
            card = build_casual_response_card(
                content_md=full_text,
                user_msg=(event.message_str or "").strip(),
            )

            stream = await send_card_via_runtime(
                streamer,
                card_type="casual_reply",
                chat_id=chat_id,
                receive_id_type=receive_id_type,
                card=card,
                platform_id=platform_id,
                event="start",
                detail=detail,
            )
            if not stream:
                return False
            s = streamer.get_stream(stream.message_id)
            if s:
                s.finalized = True

            _consume_rendered_result(result)
            logger.info(
                "[daily_card_renderer] 轻量回复转卡片 platform=%s chat=%s len=%d detail=%s",
                platform_id,
                chat_id[:20],
                len(full_text),
                detail,
            )
            return True

        # Casual replies skip waiting cards but still render lightweight cards.
        if _should_render_casual_card(event, full_text):
            await _send_casual_card("daily renderer casual reply")
            return

        # Without a waiting card, long replies use detailed cards and short
        # replies use lightweight cards.
        if not _is_card_worthy(full_text):
            await _send_casual_card("daily renderer short reply fallback")
            return

        raw_msg = getattr(event.message_obj, "raw_message", None)
        chat_id = getattr(raw_msg, "chat_id", None) or ""
        if not chat_id:
            chat_id = event.get_group_id() or event.get_sender_id() or ""
        if not chat_id:
            return
        receive_id_type = "chat_id" if chat_id.startswith("oc_") else "open_id"

        title = _extract_title(full_text) or "📋 详细分析"
        header_color = "blue"
        if any(kw in full_text[:200] for kw in ("⚠️", "风险", "警告", "失败")):
            header_color = "orange"
        elif any(kw in full_text[:200] for kw in ("✅", "完成", "成功", "通过")):
            header_color = "green"

        card = build_daily_response_card(
            content_md=full_text,
            title=title,
            header_color=header_color,
        )

        stream = await send_card_via_runtime(
            streamer,
            card_type="daily_response",
            chat_id=chat_id,
            receive_id_type=receive_id_type,
            card=card,
            platform_id=platform_id,
            event="start",
            detail="daily renderer long response",
        )
        if not stream:
            return
        s = streamer.get_stream(stream.message_id)
        if s:
            s.finalized = True

        _consume_rendered_result(result)
        logger.info(
            "[daily_card_renderer] 长回复转卡片 platform=%s chat=%s len=%d title=%r",
            platform_id,
            chat_id[:20],
            len(full_text),
            title[:30],
        )
