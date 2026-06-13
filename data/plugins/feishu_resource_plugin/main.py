"""W3 / 2A-3 飞书资料查询 Star 插件（v1 真 API 集成版）。

群里 @DC-Agent + 关键词 "查/找/资料/翻一下" → 在白名单内检索 → markdown 回群

自动模式切换：
- ``data/feishu_whitelist.yaml`` 配了 ``feishu.app_id`` + ``feishu.app_secret``
  → 走 v1：真拉 docx / bitable 内容做关键词检索
- 凭证缺失 → 退到 v0：只匹配白名单元信息（name/description）

白名单 yaml 示例见 ``data/feishu_whitelist.example.yaml``。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from dc_engines.card_runtime import send_card_via_runtime
from dc_engines.feishu_card_streamer import (
    build_daily_response_card,
    build_error_card,
    ensure_streamers_on_context,
    extract_chat_info_from_event,
)
from dc_engines.feishu_reader import (
    FeishuClient,
    Whitelist,
    load_whitelist,
    query_resources_v0,
    query_resources_v1,
)

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.star import Context, Star, register

# 触发关键词
_QUERY_KEYWORDS: tuple[str, ...] = (
    "查找",
    "查询",
    "搜索",
    "查",
    "找",
    "翻",
    "资料",
    "search",
    "look up",
)

_ATTACHMENT_SUMMARY_BLOCK_RE = re.compile(
    r"\s*<attachment_summary>.*?</attachment_summary>\s*",
    re.IGNORECASE | re.DOTALL,
)
_ATTACHMENT_SUMMARY_TAIL_RE = re.compile(
    r"\s*\[attachment_summary\].*\Z",
    re.IGNORECASE | re.DOTALL,
)
_QUESTION_PREFIXES: tuple[str, ...] = (
    "为什么",
    "为啥",
    "怎么",
    "如何",
    "哪里",
    "哪儿",
    "是否",
    "是不是",
)
_RESOURCE_QUERY_RE = re.compile(
    r"(帮我|请|麻烦|劳烦|给我|从|在).{0,12}(查|找|翻|搜索)"
    r"|"
    r"(查|找|翻|搜索).{0,12}(资料|文档|表格|文件|手册|白名单|知识库)"
    r"|"
    r"(资料库|知识库|白名单).{0,12}(查|找|搜索)",
    re.IGNORECASE,
)
_FEISHU_URL_RE = re.compile(r"(?:feishu\.cn|larksuite\.com)", re.IGNORECASE)
_ANALYSIS_INTENT_RE = re.compile(
    r"(解读|总结|分析|提炼|梳理|输出|生成|写|案例|方案|文案|海报|报告)"
)
_HARNESS_TERMINAL_STATUSES = {"completed", "cancelled", "failed"}
_HARNESS_STATUS_CAN_BLOCK_ON_MISSING_RESOURCE = {"pending", "in_progress", "blocked"}


def _merge_harness_source_citations(existing, hits: list[dict]) -> list[dict]:
    existing_citations = existing if isinstance(existing, list) else []
    merged: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add_citation(candidate: dict) -> None:
        title = str(candidate.get("title") or candidate.get("id") or "")
        source_path = str(
            candidate.get("source_path")
            or candidate.get("url")
            or candidate.get("id")
            or title
        )
        if not source_path and not title:
            return
        key = (source_path, title)
        if key in seen:
            return
        seen.add(key)
        item = dict(candidate)
        item.setdefault("title", title)
        item.setdefault("source_path", source_path)
        merged.append(item)

    for citation in existing_citations:
        if isinstance(citation, dict):
            add_citation(citation)
    for hit in hits:
        add_citation(
            {
                "title": hit.get("title", ""),
                "source_path": hit.get("url") or hit.get("id") or "",
                "source_type": hit.get("source_type", ""),
                "retrieval_mode": hit.get("retrieval_mode", ""),
                "credential_status": hit.get("credential_status", ""),
                "content_status": hit.get("content_status", ""),
                "matched_snippet": hit.get("matched_snippet", ""),
            }
        )
    return merged[:20]


def _extract_user_query_text(text: str) -> str:
    """Return the human-authored query, excluding router attachment summaries."""
    cleaned = _ATTACHMENT_SUMMARY_BLOCK_RE.sub("\n", text or "")
    cleaned = _ATTACHMENT_SUMMARY_TAIL_RE.sub("", cleaned)
    return cleaned.strip()


def _should_handle_resource_query(text: str) -> bool:
    """Keep the resource plugin conservative so normal Q&A can reach the LLM."""
    t = _extract_user_query_text(text)
    if not t:
        return False
    lowered = t.lower()
    if any(t.startswith(prefix) for prefix in _QUESTION_PREFIXES):
        return False
    if _FEISHU_URL_RE.search(t) and _ANALYSIS_INTENT_RE.search(t):
        return False
    if any(lowered.startswith(kw.lower()) for kw in _QUERY_KEYWORDS):
        return True
    return bool(_RESOURCE_QUERY_RE.search(t))


@register(
    "feishu_resource_plugin",
    "dc_agent",
    "飞书资料查询（W3 / 2A-3，v1 真 API 集成，凭证缺失自动退到 v0 元信息匹配）",
    "1.1.0",
)
class FeishuResourcePlugin(Star):
    def __init__(self, context: Context) -> None:
        super().__init__(context)
        self.whitelist: Whitelist = Whitelist()
        self.client: FeishuClient | None = None
        self.mode: str = "v0"

    async def initialize(self) -> None:
        data_dir = Path(__file__).resolve().parents[3] / "data"
        wl_path = data_dir / "feishu_whitelist.yaml"
        self.whitelist, creds = load_whitelist(wl_path)

        if creds and creds.enable:
            try:
                self.client = FeishuClient(creds)
                self.mode = "v1" if self.client.enabled else "metadata_only"
            except Exception as exc:  # noqa: BLE001
                logger.warning("[feishu_resource] FeishuClient 初始化失败：%s", exc)
                self.client = None
                self.mode = "metadata_only"
        elif creds:
            self.client = None
            self.mode = "metadata_only"
        else:
            self.client = None
            self.mode = "v0"

        logger.info(
            "[feishu_resource] 启动 mode=%s 白名单 %d 条（doc %d / table %d / folder %d）来自 %s",
            self.mode,
            self.whitelist.total(),
            len(self.whitelist.documents),
            len(self.whitelist.tables),
            len(self.whitelist.folders),
            wl_path,
        )

    def _reply(self, event: AstrMessageEvent, text: str, stop: bool = True) -> None:
        result = MessageEventResult().message(text).use_t2i(False)
        if stop:
            result.stop_event()
        event.set_result(result)

    async def _send_card(self, event: AstrMessageEvent, card: dict) -> bool:
        streamer = ensure_streamers_on_context(self.context).get(
            event.get_platform_id() or ""
        )
        chat_id, receive_id_type = extract_chat_info_from_event(event)
        if streamer is None or not chat_id:
            return False
        stream = await send_card_via_runtime(
            streamer,
            card_type="daily_response",
            chat_id=chat_id,
            receive_id_type=receive_id_type,
            card=card,
            platform_id=event.get_platform_id() or "",
            event="start",
            detail="feishu resource query card",
        )
        return stream is not None

    async def _reply_card_or_text(
        self,
        event: AstrMessageEvent,
        *,
        card: dict,
        fallback_text: str,
    ) -> None:
        if await self._send_card(event, card):
            event.stop_event()
            return
        self._reply(event, fallback_text)

    async def _load_harness_tasks_for_event(self, event: AstrMessageEvent):
        harness_engine = getattr(self.context, "harness_engine", None)
        store = getattr(harness_engine, "store", None)
        if harness_engine is None or store is None:
            return []

        task_ids: list[str] = []
        for key in ("department_workflow_task_id", "dc_truth_intake_task_id"):
            raw_value = event.get_extra(key)
            values = (
                raw_value if isinstance(raw_value, (list, tuple, set)) else [raw_value]
            )
            for value in values:
                if isinstance(value, str) and value.strip():
                    task_ids.append(value.strip())

        def _is_relevant(task) -> bool:
            return (
                task is not None
                and task.status not in _HARNESS_TERMINAL_STATUSES
                and (
                    task.domain == "truth_intake"
                    or str(task.domain).startswith("department_workflow:")
                    or str(task.domain).startswith("content_sop:")
                )
            )

        if task_ids:
            tasks = []
            for task_id in dict.fromkeys(task_ids):
                try:
                    task = await store.get_task(task_id)
                except Exception:  # noqa: BLE001
                    task = None
                if _is_relevant(task):
                    tasks.append(task)
            return tasks

        try:
            conv_id = await self.context.conversation_manager.get_curr_conversation_id(
                event.unified_msg_origin
            )
            if not conv_id:
                return []
            recent = await store.list_tasks_for_conversation(conv_id, limit=5)
        except Exception:  # noqa: BLE001
            return []
        return [task for task in recent if _is_relevant(task)]

    async def _record_harness_resource_result(
        self,
        event: AstrMessageEvent,
        *,
        status: str,
        summary: str,
        keyword: str,
        hits: list | None = None,
        retrieval_scope: str | None = None,
    ) -> None:
        harness_engine = getattr(self.context, "harness_engine", None)
        if harness_engine is None:
            return
        tasks = await self._load_harness_tasks_for_event(event)
        if not tasks:
            return

        hits = hits or []
        harness_hits = [
            {
                "type": h.source_type,
                "source_type": h.source_type,
                "id": h.source_id,
                "title": h.title,
                "url": h.url or "",
                "retrieval_mode": h.metadata.get("retrieval_mode", ""),
                "matched_snippet": h.matched_snippet,
                "credential_status": h.metadata.get("credential_status", ""),
                "matched_field": h.matched_field,
                "content_status": h.metadata.get("content_status", ""),
                "error_type": h.metadata.get("error_type", ""),
            }
            for h in hits[:10]
        ]
        result = {
            "summary": summary,
            "response_preview": summary,
            "source": "feishu_resource_plugin",
            "quality": "resource_evidence" if hits else "blocked",
            "keyword": keyword,
            "mode": self.mode,
            "retrieval_scope": retrieval_scope or self._retrieval_scope(),
            "content_status": "hits_found" if hits else "not_found",
            "harness_settlement": "evidence_attached" if hits else "blocked",
            "hits": harness_hits,
        }
        for task in tasks:
            try:
                if hits:
                    await self._attach_harness_resource_evidence(
                        harness_engine,
                        task,
                        result,
                    )
                    continue
                if task.status not in _HARNESS_STATUS_CAN_BLOCK_ON_MISSING_RESOURCE:
                    await harness_engine.append_trace(
                        task.task_id,
                        "feishu_resource_missing_skipped_status_change",
                        result,
                    )
                    continue
                await harness_engine.set_status(
                    task.task_id,
                    "blocked",
                    result=result,
                    event_payload={"reason": summary[:200]},
                )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "[feishu_resource] harness settle failed task=%s",
                    task.task_id,
                    exc_info=True,
                )

    async def _attach_harness_resource_evidence(
        self,
        harness_engine,
        task,
        result: dict,
    ) -> None:
        task_payload = getattr(task, "payload", {}) or {}
        existing_citations = task_payload.get("source_citations") or []
        citations = _merge_harness_source_citations(existing_citations, result["hits"])
        patch = {
            "resource_retrieval_status": "evidence_attached",
            "resource_retrieval_summary": result["summary"],
            "resource_retrieval_keyword": result["keyword"],
            "retrieval_mode": result["mode"],
            "retrieval_scope": result["retrieval_scope"],
            "content_status": result["content_status"],
            "feishu_resource_hits": result["hits"],
            "source_citations": citations,
        }
        await harness_engine.merge_payload(
            task.task_id,
            patch,
            event_type="feishu_resource_evidence_attached",
        )
        await harness_engine.append_trace(
            task.task_id,
            "feishu_resource_retrieval_result",
            result,
        )

    def _resource_scope_text(self) -> str:
        return (
            f"白名单 {self.whitelist.total()} 条"
            f"（doc {len(self.whitelist.documents)} / "
            f"table {len(self.whitelist.tables)} / "
            f"folder {len(self.whitelist.folders)}），模式 {self.mode}"
        )

    def _metadata_query_credential_status(self) -> str:
        if self.mode == "metadata_only":
            return "disabled"
        return "missing"

    def _retrieval_scope(self) -> str:
        if self.mode == "v1":
            return "bounded_content_with_metadata_fallback"
        return "metadata_only"

    def _no_hits_summary(self, keyword: str) -> str:
        if self.mode == "v1":
            return (
                f"有限内容检索与元信息回退范围内未找到「{keyword}」的匹配资料。"
                "可换更短关键词或提供飞书链接继续处理。"
            )
        return f"没有在资料元信息里找到「{keyword}」的匹配内容。"

    def _no_hits_fallback_text(self, keyword: str) -> str:
        base = (
            f"没找到含「{keyword}」的资料"
            f"（白名单 {self.whitelist.total()} 条，模式 {self.mode}）"
        )
        if self.mode == "v1":
            return base + "；已进行有限内容检索并回退检查元信息。"
        return base

    def _build_no_hits_card(self, keyword: str) -> dict:
        scope_note = ""
        if self.mode == "v1":
            scope_note = "\n\n**检索说明**：内容检索按元信息预筛并限制源数量；未命中不等于所有授权资料全文都包含不了该词。"
        return build_daily_response_card(
            title="📚 资料查询未命中",
            header_color="orange",
            content_md=(
                f"**查询词**：`{keyword}`\n\n"
                f"**检索范围**：{self._resource_scope_text()}\n\n"
                f"{self._no_hits_summary(keyword)}"
                f"{scope_note}"
            ),
            footer_hint="如果你是在问图片或截图里的内容，直接提问即可，不需要触发资料查询。",
        )

    def _build_empty_whitelist_card(self) -> dict:
        return build_error_card(
            title="资料库未配置",
            error_msg="白名单未配置任何文档、表格或 KB 文件夹。",
            retry_hint="请联系管理员编辑 `data/feishu_whitelist.yaml`（参考 `feishu_whitelist.example.yaml`）。",
        )

    def _extract_keyword(self, text: str) -> str:
        t = text.strip()
        for kw in _QUERY_KEYWORDS:
            if t.startswith(kw):
                t = t[len(kw) :].strip(" 一下 :：,，")
                break
            if kw in t:
                idx = t.find(kw)
                t = t[idx + len(kw) :].strip(" 一下 :：,，")
                break
        return t.strip()

    def _build_hits_reply_text(self, keyword: str, hits: list) -> str:
        lines = [f"🔍 「{keyword}」找到 {len(hits)} 条相关资料（{self.mode}）："]
        for h in hits:
            kind_emoji = {"document": "📄", "table": "📊", "folder": "📁"}.get(
                h.source_type, "•"
            )
            score_bar = "★" * min(5, int(h.score * 5))
            metadata = h.metadata or {}
            retrieval_mode = metadata.get("retrieval_mode") or (
                "content" if h.matched_field == "content" else "metadata_only"
            )
            credential_status = metadata.get("credential_status", "unknown")
            content_status = metadata.get("content_status", "unknown")
            error_type = metadata.get("error_type", "")
            provenance_parts = [
                f"来源: {h.source_type}",
                f"检索: {retrieval_mode}",
                f"凭证: {credential_status}",
                f"字段: {h.matched_field or 'unknown'}",
                f"状态: {content_status}",
            ]
            if error_type:
                provenance_parts.append(f"错误: {error_type}")

            lines.append(f"\n{kind_emoji} **{h.title}** ({h.domain}) {score_bar}")
            lines.append("   " + " / ".join(provenance_parts))
            if h.matched_snippet:
                lines.append(f"   匹配: {h.matched_snippet}")
            elif h.summary:
                lines.append(f"   {h.summary[:80]}")
            if h.url:
                lines.append(f"   [打开]({h.url})")
        if self.mode != "v1":
            lines.append("")
            lines.append(
                "_注：当前仅匹配资料元信息；可用 Feishu 凭证启用后才会进行真实内容检索_"
            )
        return "\n".join(lines)

    @filter.event_message_type(
        EventMessageType.GROUP_MESSAGE | EventMessageType.PRIVATE_MESSAGE
    )
    async def on_message(self, event: AstrMessageEvent):
        raw_text = (event.message_str or "").strip()
        text = _extract_user_query_text(raw_text)
        if not text:
            return

        is_group = "GroupMessage" in (event.unified_msg_origin or "")
        if is_group and not getattr(event, "is_at_or_wake_command", False):
            return

        if not _should_handle_resource_query(raw_text):
            return

        keyword = self._extract_keyword(text)
        if not keyword or len(keyword) < 2:
            return

        # domain hint
        domain_hint: str | None = None
        case_engine = getattr(self.context, "case_engine", None)
        active_case = None
        if case_engine is not None:
            try:
                active_case = await case_engine.get_current_case_for_session(
                    event.unified_msg_origin
                )
                if active_case is not None:
                    domain_hint = (active_case.client_name or "").lower() or None
            except Exception:  # noqa: BLE001
                pass

        # v0 / v1 自动切换
        if self.mode == "v1" and self.client is not None and self.client.enabled:
            hits = await query_resources_v1(
                keyword,
                whitelist=self.whitelist,
                client=self.client,
                domain_hint=domain_hint,
            )
        else:
            hits = query_resources_v0(
                keyword,
                whitelist=self.whitelist,
                domain_hint=domain_hint,
                credential_status=self._metadata_query_credential_status(),
            )

        if not hits:
            if self.whitelist.total() == 0:
                fallback = (
                    "⚠️ 白名单未配置任何文档/表格/KB。\n"
                    "请联系管理员编辑 `data/feishu_whitelist.yaml`（参考 `feishu_whitelist.example.yaml`）"
                )
                await self._reply_card_or_text(
                    event,
                    card=self._build_empty_whitelist_card(),
                    fallback_text=fallback,
                )
                await self._record_harness_resource_result(
                    event,
                    status="blocked",
                    summary="资料库未配置，无法返回资料查询结果。",
                    keyword=keyword,
                )
            else:
                fallback = self._no_hits_fallback_text(keyword)
                await self._reply_card_or_text(
                    event,
                    card=self._build_no_hits_card(keyword),
                    fallback_text=fallback,
                )
                await self._record_harness_resource_result(
                    event,
                    status="blocked",
                    summary=self._no_hits_summary(keyword),
                    keyword=keyword,
                    retrieval_scope=self._retrieval_scope(),
                )
            return

        reply_text = self._build_hits_reply_text(keyword, hits)
        await self._reply_card_or_text(
            event,
            card=build_daily_response_card(
                title="🔍 资料查询结果",
                header_color="blue",
                content_md=reply_text,
                footer_hint=f"检索范围：{self._resource_scope_text()}",
            ),
            fallback_text=reply_text,
        )
        await self._record_harness_resource_result(
            event,
            status="evidence_attached",
            summary=f"已返回「{keyword}」的资料查询结果，共 {len(hits)} 条。",
            keyword=keyword,
            hits=hits,
        )

        # case event
        if active_case is not None and case_engine is not None:
            try:
                await case_engine.store.append_event(
                    active_case.case_id,
                    "resource_query_logged",
                    {
                        "keyword": keyword,
                        "mode": self.mode,
                        "hits": [
                            {
                                "type": h.source_type,
                                "id": h.source_id,
                                "title": h.title,
                                "retrieval_mode": h.metadata.get("retrieval_mode", ""),
                                "credential_status": h.metadata.get(
                                    "credential_status", ""
                                ),
                                "content_status": h.metadata.get("content_status", ""),
                                "error_type": h.metadata.get("error_type", ""),
                            }
                            for h in hits
                        ],
                        "ts": datetime.now(timezone.utc).isoformat(),
                    },
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("[feishu_resource] case event 写入失败：%s", exc)

        logger.info(
            "[feishu_resource] umo=%s mode=%s keyword=%r hits=%d",
            event.unified_msg_origin,
            self.mode,
            keyword,
            len(hits),
        )
