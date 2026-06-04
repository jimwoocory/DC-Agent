"""LLM 回复自动渲染成飞书 interactive card。

关键钩子：`@filter.on_decorating_result()` —— LLM 出 result 后、AstrBot
还没发出去之前介入。

流程：
1. LLM 排队/调用前为任务类请求发等待卡；闲聊保持飞书原生气泡
2. LLM 出结果后优先把等待卡 finalize 成回复卡，**清空原 result**
   避免 AstrBot 重复发纯文本
3. 若没有等待卡，则长结构化内容仍会兜底转卡片

只对 lark 平台生效（需要 feishu_streamers 已挂在 context 上）。
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

# 占位卡 stream_id 在 event 上的 key（on_llm_request 写入，on_decorating_result 读）
_STREAM_KEY = "_daily_card_thinking_stream_id"
_BRIEF_KEY = "_daily_card_user_brief"

# 等待卡只给明确任务使用；普通闲聊用飞书原生气泡更自然。
MIN_THINKING_PLACEHOLDER_CHARS = 1

# 触发卡片渲染的最小字符数（降低门槛让中等长度也享受卡片）
MIN_CARD_CHARS = 150

# 结构化 markdown 标记（任一存在就视为长结构化内容）
_STRUCTURE_MARKERS = re.compile(
    r"^#{1,4} |\*\*[^*]+\*\*|^\|.+\|.+\||^- |^\d+\. |^> ",
    re.MULTILINE,
)

# 给 LLM 的格式偏好（注入到 system_prompt 尾部，鼓励多用 markdown）
_FORMAT_HINT = (
    "\n\n## 📋 回复格式偏好（飞书展示用）\n"
    "- 超过 80 字的回复，请用 markdown 结构化：用 `## 章节标题` 分块、"
    "关键点用 `**加粗**`、清单用列表（`- xxx`）、数据用表格（`| a | b |`）。\n"
    "- 短回复（80 字以内）保持自然口语，不强行加 markdown。\n"
    "- 重要结论可放在第一段开头，让用户一眼看见。\n"
    "- 多段内容时，每段之间用空行分隔（飞书卡片按空行切段渲染）。"
)


def _is_card_worthy(text: str) -> bool:
    """判断是否值得渲染成卡片：长度 ≥ 150 + 至少 1 个结构化标记。"""
    if not text or len(text) < MIN_CARD_CHARS:
        return False
    return bool(_STRUCTURE_MARKERS.search(text))


def _extract_title(text: str) -> str | None:
    """从 markdown 抽第一个 # 标题作为卡片头部。没有则返 None。"""
    for line in text.split("\n", 5):
        s = line.strip()
        if s.startswith("# "):
            return s.lstrip("# ").strip()[:50]
        if s.startswith("## "):
            return s.lstrip("# ").strip()[:50]
    return None


def _should_use_waiting_card(event: AstrMessageEvent) -> bool:
    intent = str(event.get_extra("llm_router_intent") or "").strip()
    return should_start_waiting_card(
        intent=intent,
        message=event.message_str or "",
        reasoning_tier=event.get_extra("reasoning_tier"),
    )


def _should_render_casual_card(event: AstrMessageEvent, text: str) -> bool:
    intent = str(event.get_extra("llm_router_intent") or "").strip()
    return should_render_casual_reply_card(
        intent=intent,
        message=event.message_str or "",
    )


@register(
    "daily_card_renderer",
    "dc_agent",
    "LLM 长回复自动渲染成飞书 interactive card（级别 3）",
    "1.1.0",
)
class DailyCardRendererPlugin(Star):
    def __init__(self, context: Context) -> None:
        super().__init__(context)
        self._log_card_system_health()

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
        logger.error(
            "[card_system] health FAILED: %s details=%s",
            ", ".join(failed),
            report.details,
        )

    async def _start_thinking_card_if_needed(self, event: AstrMessageEvent) -> None:
        """Create one waiting card for a lark LLM request if it does not exist yet."""
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
        """LLM 排队等锁前发等待卡，避免锁等待期间没有任何视觉反馈。"""
        await self._start_thinking_card_if_needed(event)

    @filter.on_llm_request()
    async def inject_format_preference_and_ensure_thinking_card(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> None:
        """LLM 调用前两件事：
        1. 注入「回复用 markdown 结构化」偏好（鼓励 LLM 出长结构化内容）
        2. 兜底确保"思考中"卡片已创建 —— 兼容绕过等待钩子的旧路径
        """
        req.system_prompt = (req.system_prompt or "") + _FORMAT_HINT
        await self._start_thinking_card_if_needed(event)

    @filter.on_decorating_result()
    async def finalize_or_render_card(
        self,
        event: AstrMessageEvent,
    ) -> None:
        """LLM 出结果后：
        - 有占位卡（_STREAM_KEY 已存）→ finalize 替换为结果卡 + clear 原 result
        - 没占位卡 + 长结构化内容 → 异步发新卡 + clear 原 result
        - 没占位卡 + 短内容 → 不动，让 AstrBot 默认发纯文本
        """
        platform_id = event.get_platform_id() or ""
        if not platform_id:
            return
        streamers = ensure_streamers_on_context(self.context)
        streamer = streamers.get(platform_id)
        if streamer is None:
            return

        result = event.get_result()
        if not result or not result.chain:
            return

        # 抽 Plain 文本
        plain_parts: list[str] = []
        for comp in result.chain:
            if isinstance(comp, Plain):
                plain_parts.append(comp.text or "")
        full_text = "\n".join(plain_parts).strip()
        if not full_text:
            return

        # ─── 优先：有占位卡 → finalize 替换（所有长度都走这里，体验一致）───
        stream_id = event.get_extra(_STREAM_KEY)
        if stream_id:
            # 判断头部颜色 + 标题
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
            # clear 原 result 避免重复发纯文本
            result.chain.clear()
            result.chain.append(Plain(""))
            logger.info(
                "[daily_card_renderer] 占位卡 finalize message_id=%s len=%d",
                stream_id,
                len(full_text),
            )
            return

        # ─── 闲聊：不走任务等待卡，但回复仍用轻量闲聊卡渲染 ───
        if _should_render_casual_card(event, full_text):
            raw_msg = getattr(event.message_obj, "raw_message", None)
            chat_id = getattr(raw_msg, "chat_id", None) or ""
            if not chat_id:
                chat_id = event.get_group_id() or event.get_sender_id() or ""
            if not chat_id:
                return
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
                detail="daily renderer casual reply",
            )
            if not stream:
                return
            s = streamer.get_stream(stream.message_id)
            if s:
                s.finalized = True

            result.chain.clear()
            result.chain.append(Plain(""))
            logger.info(
                "[daily_card_renderer] 闲聊转卡片 platform=%s chat=%s len=%d",
                platform_id,
                chat_id[:20],
                len(full_text),
            )
            return

        # ─── 无占位卡（例如短任务没触发占位）→ 走旧的"长内容才转卡片"逻辑 ───
        if not _is_card_worthy(full_text):
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

        result.chain.clear()
        result.chain.append(Plain(""))
        logger.info(
            "[daily_card_renderer] 长回复转卡片 platform=%s chat=%s len=%d title=%r",
            platform_id,
            chat_id[:20],
            len(full_text),
            title[:30],
        )
