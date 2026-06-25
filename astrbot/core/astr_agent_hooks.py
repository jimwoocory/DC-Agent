import json
import re
from typing import Any

from mcp.types import CallToolResult

from astrbot.core.agent.hooks import BaseAgentRunHooks
from astrbot.core.agent.message import Message
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool
from astrbot.core.astr_agent_context import AstrAgentContext
from astrbot.core.pipeline.context_utils import call_event_hook
from astrbot.core.star.star_handler import EventType

WEB_SEARCH_TOOL_NAMES = {
    "web_search_baidu",
    "web_search_tavily",
    "web_search_bocha",
    "web_search_brave",
    "web_search_firecrawl",
}

WEB_SEARCH_EVIDENCE_EXTRA = "dc_web_search_evidence"
WEB_SEARCH_EVIDENCE_RAW_EXTRA = "dc_web_search_evidence_raw"
WEB_SEARCH_BLOCKED_MESSAGE = (
    "这条问题需要真实联网检索，但本轮回复没有通过证据校验，所以我已拦截发送。"
    "请重试一次；如果连续出现，请检查 Brave 搜索服务或检索结果是否为空。"
)
WEB_SEARCH_SOURCE_APPENDIX_HEADING = "检索来源"
_REF_RE = re.compile(r"<ref>([^<]+)</ref>")
_MAX_FALLBACK_EVIDENCE_ITEMS = 8


class MainAgentHooks(BaseAgentRunHooks[AstrAgentContext]):
    async def on_agent_begin(
        self, run_context: ContextWrapper[AstrAgentContext]
    ) -> None:
        await call_event_hook(
            run_context.context.event,
            EventType.OnAgentBeginEvent,
            run_context,
        )

    async def on_agent_done(self, run_context, llm_response) -> None:
        _enforce_web_search_fidelity(run_context.context.event, llm_response)

        # 执行事件钩子
        if llm_response and llm_response.reasoning_content:
            # we will use this in result_decorate stage to inject reasoning content to chain
            run_context.context.event.set_extra(
                "_llm_reasoning_content", llm_response.reasoning_content
            )

        await call_event_hook(
            run_context.context.event,
            EventType.OnLLMResponseEvent,
            llm_response,
        )
        await call_event_hook(
            run_context.context.event,
            EventType.OnAgentDoneEvent,
            run_context,
            llm_response,
        )

    async def on_tool_start(
        self,
        run_context: ContextWrapper[AstrAgentContext],
        tool: FunctionTool[Any],
        tool_args: dict | None,
    ) -> None:
        await call_event_hook(
            run_context.context.event,
            EventType.OnUsingLLMToolEvent,
            tool,
            tool_args,
        )

    async def on_tool_end(
        self,
        run_context: ContextWrapper[AstrAgentContext],
        tool: FunctionTool[Any],
        tool_args: dict | None,
        tool_result: CallToolResult | None,
    ) -> None:
        run_context.context.event.clear_result()
        await call_event_hook(
            run_context.context.event,
            EventType.OnLLMToolRespondEvent,
            tool,
            tool_args,
            tool_result,
        )

        if tool.name in WEB_SEARCH_TOOL_NAMES:
            run_context.context.event.set_extra("dc_web_search_used", True)
            run_context.context.event.set_extra("dc_web_search_tool", tool.name)
            _store_web_search_evidence(
                run_context.context.event,
                tool_name=tool.name,
                tool_args=tool_args,
                tool_result=tool_result,
            )

        # special handle web_search_tavily
        platform_name = run_context.context.event.get_platform_name()
        if (
            platform_name == "webchat"
            and tool.name in WEB_SEARCH_TOOL_NAMES
            and len(run_context.messages) > 0
            and tool_result
            and len(tool_result.content)
        ):
            # inject system prompt
            first_part = run_context.messages[0]
            if (
                isinstance(first_part, Message)
                and first_part.role == "system"
                and first_part.content
                and isinstance(first_part.content, str)
            ):
                # we assume system part is str
                first_part.content += (
                    "Always cite web search results you rely on. "
                    "Index is a unique identifier for each search result. "
                    "Use the exact citation format <ref>index</ref> (e.g. <ref>abcd.3</ref>) "
                    "after the sentence that uses the information. Do not invent citations."
                )


class EmptyAgentHooks(BaseAgentRunHooks[AstrAgentContext]):
    pass


def _store_web_search_evidence(
    event,
    *,
    tool_name: str,
    tool_args: dict | None,
    tool_result: CallToolResult | None,
) -> None:
    raw_text = _tool_result_text(tool_result)
    if not raw_text:
        return

    existing_raw = event.get_extra(WEB_SEARCH_EVIDENCE_RAW_EXTRA, []) or []
    if not isinstance(existing_raw, list):
        existing_raw = []
    existing_raw.append(
        {
            "tool": tool_name,
            "query": _search_query_from_args(tool_args),
            "raw": raw_text,
        }
    )
    event.set_extra(WEB_SEARCH_EVIDENCE_RAW_EXTRA, existing_raw)

    parsed_results = _parse_search_results(raw_text)
    if not parsed_results:
        return

    existing = event.get_extra(WEB_SEARCH_EVIDENCE_EXTRA, []) or []
    if not isinstance(existing, list):
        existing = []
    existing.extend(
        {
            "tool": tool_name,
            "query": _search_query_from_args(tool_args),
            **result,
        }
        for result in parsed_results
    )
    event.set_extra(WEB_SEARCH_EVIDENCE_EXTRA, existing)
    event.set_extra("dc_web_search_evidence_locked", True)


def _tool_result_text(tool_result: CallToolResult | None) -> str:
    parts: list[str] = []
    for content in getattr(tool_result, "content", []) or []:
        text = getattr(content, "text", None)
        if isinstance(text, str):
            parts.append(text)
        elif isinstance(content, str):
            parts.append(content)
    return "\n".join(part for part in parts if part)


def _parse_search_results(raw_text: str) -> list[dict[str, str]]:
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError:
        return []
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        return []

    parsed: list[dict[str, str]] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        parsed.append(
            {
                "index": str(item.get("index", "")),
                "title": str(item.get("title", "")),
                "url": str(item.get("url", "")),
                "snippet": str(item.get("snippet", "")),
            }
        )
    return parsed


def _search_query_from_args(tool_args: dict | None) -> str:
    if not isinstance(tool_args, dict):
        return ""
    return str(tool_args.get("query") or tool_args.get("q") or "")


def _enforce_web_search_fidelity(event, llm_response) -> None:
    if not _event_extra_true(event, "dc_router_meta_search_required"):
        return
    if llm_response is None or getattr(llm_response, "role", "") != "assistant":
        return

    evidence = event.get_extra(WEB_SEARCH_EVIDENCE_EXTRA, []) or []
    evidence_by_index = _evidence_by_index(evidence)
    evidence_indexes = set(evidence_by_index)
    used_search = _event_extra_true(event, "dc_web_search_used")
    evidence_locked = _event_extra_true(event, "dc_web_search_evidence_locked")
    answer_text = str(getattr(llm_response, "completion_text", "") or "")
    refs = [ref.strip() for ref in _REF_RE.findall(answer_text) if ref.strip()]

    reason = ""
    if not used_search:
        reason = "search_not_called"
    elif not evidence_locked or not evidence_indexes:
        reason = "evidence_not_locked"
    elif not refs:
        reason = "missing_citation"
    else:
        unknown_refs = sorted(set(refs) - evidence_indexes)
        if unknown_refs:
            reason = f"unknown_citation:{','.join(unknown_refs)}"

    if not reason:
        llm_response.completion_text = _append_web_search_source_appendix(
            answer_text,
            refs=refs,
            evidence_by_index=evidence_by_index,
        )
        event.set_extra("dc_web_search_sources_appended", True)
        event.set_extra("dc_web_search_guardrail", "passed")
        return

    if used_search and evidence_locked and evidence_indexes:
        llm_response.completion_text = _build_web_search_evidence_summary(
            reason=reason,
            evidence=evidence,
        )
        event.set_extra("dc_web_search_guardrail", "fallback_evidence_summary")
        event.set_extra("dc_web_search_guardrail_reason", reason)
        event.set_extra("dc_web_search_sources_appended", True)
        return

    llm_response.completion_text = WEB_SEARCH_BLOCKED_MESSAGE
    event.set_extra("dc_web_search_guardrail", "blocked")
    event.set_extra("dc_web_search_guardrail_reason", reason)


def _event_extra_true(event, key: str) -> bool:
    raw_value = event.get_extra(key)
    if isinstance(raw_value, bool):
        return raw_value
    return str(raw_value or "").strip().lower() in {"1", "true", "yes", "on"}


def _evidence_by_index(evidence: object) -> dict[str, dict]:
    if not isinstance(evidence, list):
        return {}
    indexed: dict[str, dict] = {}
    for item in evidence:
        if not isinstance(item, dict):
            continue
        index = str(item.get("index", "")).strip()
        if index and index not in indexed:
            indexed[index] = item
    return indexed


def _append_web_search_source_appendix(
    answer_text: str,
    *,
    refs: list[str],
    evidence_by_index: dict[str, dict],
) -> str:
    if WEB_SEARCH_SOURCE_APPENDIX_HEADING in answer_text:
        return answer_text

    ordered_refs = list(dict.fromkeys(refs))
    lines: list[str] = []
    for index in ordered_refs:
        item = evidence_by_index.get(index)
        if not item:
            continue
        title = _clean_source_field(item.get("title"))
        url = _clean_source_field(item.get("url"))
        snippet = _clean_source_field(item.get("snippet"))
        lines.append(f"- [{index}] {title or 'Untitled'}")
        if url:
            lines.append(f"  URL: {url}")
        if snippet:
            lines.append(f"  摘要: {snippet}")

    if not lines:
        return answer_text
    return (
        f"{answer_text.rstrip()}\n\n{WEB_SEARCH_SOURCE_APPENDIX_HEADING}\n"
        + "\n".join(lines)
    )


def _build_web_search_evidence_summary(
    *,
    reason: str,
    evidence: object,
) -> str:
    if not isinstance(evidence, list):
        evidence = []

    lines = [
        "我已完成联网检索，但模型本轮没有产出合规的最终引用正文；"
        "为避免编造，我先按已锁定的搜索结果返回保守摘要。",
        "",
        "可用检索结果",
    ]
    seen: set[str] = set()
    count = 0
    for item in evidence:
        if not isinstance(item, dict):
            continue
        index = _clean_source_field(item.get("index"))
        if not index or index in seen:
            continue
        seen.add(index)
        title = _clean_source_field(item.get("title")) or "Untitled"
        url = _clean_source_field(item.get("url"))
        snippet = _clean_source_field(item.get("snippet"))
        lines.append(f"- [{index}] {title}")
        if snippet:
            lines.append(f"  摘要: {snippet}")
        if url:
            lines.append(f"  URL: {url}")
        count += 1
        if count >= _MAX_FALLBACK_EVIDENCE_ITEMS:
            break

    if count == 0:
        return WEB_SEARCH_BLOCKED_MESSAGE

    lines.extend(
        [
            "",
            f"保真状态: 已搜索并锁定来源，但最终正文未通过引用格式校验（{reason}）。",
            "建议: 可以让我基于以上来源继续整理正式报告。",
        ]
    )
    return "\n".join(lines)


def _clean_source_field(value: object) -> str:
    return str(value or "").replace("\n", " ").strip()


MAIN_AGENT_HOOKS = MainAgentHooks()
