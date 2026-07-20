"""Tests for main agent hook side effects."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp.types import CallToolResult, TextContent

from astrbot.core.astr_agent_hooks import MainAgentHooks
from astrbot.core.provider.entities import LLMResponse


@pytest.mark.asyncio
async def test_web_search_tool_call_sets_audit_extras() -> None:
    """Search tool execution must leave an audit marker on the event."""
    event = MagicMock()
    event.get_platform_name.return_value = "lark"
    run_context = SimpleNamespace(
        context=SimpleNamespace(event=event),
        messages=[],
    )
    tool = MagicMock()
    tool.name = "web_search_brave"

    with patch(
        "astrbot.core.astr_agent_hooks.call_event_hook",
        new=AsyncMock(),
    ):
        await MainAgentHooks().on_tool_end(run_context, tool, {}, None)

    event.set_extra.assert_any_call("dc_web_search_used", True)
    event.set_extra.assert_any_call("dc_web_search_tool", "web_search_brave")


@pytest.mark.asyncio
async def test_web_search_tool_call_locks_raw_and_parsed_evidence() -> None:
    """Search result JSON is preserved separately from the LLM final wording."""
    event = MagicMock()
    event.get_platform_name.return_value = "lark"
    event.get_extra.side_effect = lambda key, default=None: default
    run_context = SimpleNamespace(
        context=SimpleNamespace(event=event),
        messages=[],
    )
    tool = MagicMock()
    tool.name = "web_search_brave"
    result = CallToolResult(
        content=[
            TextContent(
                type="text",
                text=(
                    '{"results":[{"title":"Title A","url":"https://example.com",'
                    '"snippet":"Exact snippet 42%","index":"abcd.1"}]}'
                ),
            )
        ]
    )

    with patch(
        "astrbot.core.astr_agent_hooks.call_event_hook",
        new=AsyncMock(),
    ):
        await MainAgentHooks().on_tool_end(
            run_context,
            tool,
            {"query": "小红书趋势"},
            result,
        )

    raw_call = next(
        call
        for call in event.set_extra.call_args_list
        if call.args[0] == "dc_web_search_evidence_raw"
    )
    evidence_call = next(
        call
        for call in event.set_extra.call_args_list
        if call.args[0] == "dc_web_search_evidence"
    )
    assert raw_call.args[1][0]["raw"] == result.content[0].text
    assert evidence_call.args[1] == [
        {
            "tool": "web_search_brave",
            "query": "小红书趋势",
            "index": "abcd.1",
            "title": "Title A",
            "url": "https://example.com",
            "snippet": "Exact snippet 42%",
        }
    ]
    event.set_extra.assert_any_call("dc_web_search_evidence_locked", True)


@pytest.mark.asyncio
async def test_search_required_answer_without_evidence_is_blocked() -> None:
    """Required retrieval cannot publish an answer without a completed search."""
    event = MagicMock()
    event.get_extra.side_effect = lambda key, default=None: {
        "dc_router_meta_search_required": "true",
        "dc_web_search_used": False,
    }.get(key, default)
    response = LLMResponse(role="assistant", completion_text="这是一个没有证据的答案")
    run_context = SimpleNamespace(context=SimpleNamespace(event=event))

    with patch(
        "astrbot.core.astr_agent_hooks.call_event_hook",
        new=AsyncMock(),
    ):
        await MainAgentHooks().on_agent_done(run_context, response)

    assert "没有通过证据校验" in response.completion_text
    event.set_extra.assert_any_call("dc_web_search_guardrail", "blocked")
    event.set_extra.assert_any_call(
        "dc_web_search_guardrail_reason",
        "search_not_called",
    )


@pytest.mark.asyncio
async def test_confirmed_workbench_copy_bypasses_web_search_guardrail() -> None:
    """Confirmed creative copy is allowed without retrieval evidence."""
    event = MagicMock()
    event.get_extra.side_effect = lambda key, default=None: {
        "assistant_workbench_task_type": "copy",
        "dc_router_meta_search_required": "true",
        "dc_web_search_used": False,
    }.get(key, default)
    response = LLMResponse(role="assistant", completion_text="安心检测，从专业开始。")
    run_context = SimpleNamespace(context=SimpleNamespace(event=event))

    with patch(
        "astrbot.core.astr_agent_hooks.call_event_hook",
        new=AsyncMock(),
    ):
        await MainAgentHooks().on_agent_done(run_context, response)

    assert response.completion_text == "安心检测，从专业开始。"
    assert not any(
        call.args and call.args[0] == "dc_web_search_guardrail"
        for call in event.set_extra.call_args_list
    )


@pytest.mark.asyncio
async def test_search_required_answer_with_locked_evidence_and_valid_refs_passes() -> (
    None
):
    """A source-backed answer passes when every citation points to locked evidence."""
    event = MagicMock()
    event.get_extra.side_effect = lambda key, default=None: {
        "dc_router_meta_search_required": "true",
        "dc_web_search_used": True,
        "dc_web_search_evidence_locked": True,
        "dc_web_search_evidence": [
            {
                "index": "abcd.1",
                "title": "趋势来源",
                "url": "https://example.com/trend",
                "snippet": "原始摘要 42%",
            }
        ],
    }.get(key, default)
    response = LLMResponse(
        role="assistant",
        completion_text="小红书趋势来自检索结果。<ref>abcd.1</ref>",
    )
    run_context = SimpleNamespace(context=SimpleNamespace(event=event))

    with patch(
        "astrbot.core.astr_agent_hooks.call_event_hook",
        new=AsyncMock(),
    ):
        await MainAgentHooks().on_agent_done(run_context, response)

    assert response.completion_text == (
        "小红书趋势来自检索结果。<ref>abcd.1</ref>\n\n"
        "检索来源\n"
        "- [abcd.1] 趋势来源\n"
        "  URL: https://example.com/trend\n"
        "  摘要: 原始摘要 42%"
    )
    event.set_extra.assert_any_call("dc_web_search_sources_appended", True)
    event.set_extra.assert_any_call("dc_web_search_guardrail", "passed")


@pytest.mark.asyncio
async def test_search_required_source_appendix_is_not_duplicated() -> None:
    """Source appendix is deterministic and should not be appended twice."""
    event = MagicMock()
    event.get_extra.side_effect = lambda key, default=None: {
        "dc_router_meta_search_required": "true",
        "dc_web_search_used": True,
        "dc_web_search_evidence_locked": True,
        "dc_web_search_evidence": [{"index": "abcd.1", "title": "Title"}],
    }.get(key, default)
    response = LLMResponse(
        role="assistant",
        completion_text=("结论。<ref>abcd.1</ref>\n\n检索来源\n- [abcd.1] Title"),
    )
    run_context = SimpleNamespace(context=SimpleNamespace(event=event))

    with patch(
        "astrbot.core.astr_agent_hooks.call_event_hook",
        new=AsyncMock(),
    ):
        await MainAgentHooks().on_agent_done(run_context, response)

    assert response.completion_text.count("检索来源") == 1


@pytest.mark.asyncio
async def test_search_required_empty_final_with_evidence_returns_locked_summary() -> (
    None
):
    """Search succeeded but model failed to synthesize; publish locked evidence."""
    event = MagicMock()
    event.get_extra.side_effect = lambda key, default=None: {
        "dc_router_meta_search_required": "true",
        "dc_web_search_used": True,
        "dc_web_search_evidence_locked": True,
        "dc_web_search_evidence": [
            {
                "index": "d38f.2",
                "title": "缤果 Pro 开售 13 天销量突破两万",
                "url": "https://chejiahao.autohome.com.cn/info/25637245",
                "snippet": "开售仅 13 天整车销量冲破 20000 台",
            },
            {
                "index": "636a.1",
                "title": "缤果/海鸥/星愿对比分析",
                "url": "https://chejiahao.autohome.com.cn/info/25445425",
                "snippet": "比亚迪海鸥智驾版优势鲜明",
            },
        ],
    }.get(key, default)
    response = LLMResponse(role="assistant", completion_text="")
    run_context = SimpleNamespace(context=SimpleNamespace(event=event))

    with patch(
        "astrbot.core.astr_agent_hooks.call_event_hook",
        new=AsyncMock(),
    ):
        await MainAgentHooks().on_agent_done(run_context, response)

    assert "没有通过证据校验" not in response.completion_text
    assert "可用检索结果" in response.completion_text
    assert "[d38f.2] 缤果 Pro 开售 13 天销量突破两万" in response.completion_text
    assert "missing_citation" in response.completion_text
    event.set_extra.assert_any_call(
        "dc_web_search_guardrail",
        "fallback_evidence_summary",
    )
    event.set_extra.assert_any_call(
        "dc_web_search_guardrail_reason",
        "missing_citation",
    )
