import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from astrbot.core.runtime_context.memory_query import (
    build_memory_retrieval_query,
    recent_history_text_for_memory_query,
)


def test_recent_history_text_for_memory_query_keeps_recent_user_and_assistant_turns() -> (
    None
):
    history = json.dumps(
        [
            {"role": "system", "content": "ignore"},
            {"role": "user", "content": "帮我做五菱2026中秋传播方案"},
            {
                "role": "assistant",
                "content": "五菱2026中秋，要做陪你去看月亮的那台车。",
            },
        ],
        ensure_ascii=False,
    )

    text = recent_history_text_for_memory_query(history)

    assert "system" not in text
    assert "用户: 帮我做五菱2026中秋传播方案" in text
    assert "助手: 五菱2026中秋" in text


@pytest.mark.asyncio
async def test_build_memory_retrieval_query_combines_history_and_short_feedback() -> (
    None
):
    conversation = SimpleNamespace(
        history=json.dumps(
            [
                {"role": "user", "content": "帮我做五菱2026中秋传播方案"},
                {"role": "assistant", "content": "五菱2026中秋方案已经输出。"},
            ],
            ensure_ascii=False,
        )
    )
    conv_mgr = MagicMock()
    conv_mgr.get_curr_conversation_id = AsyncMock(return_value="conv-id")
    conv_mgr.get_conversation = AsyncMock(return_value=conversation)
    context = MagicMock(conversation_manager=conv_mgr)
    event = MagicMock()
    event.message_str = "不满意"
    event.unified_msg_origin = "lark:巅池-Agent小助手:chat-id"

    query = await build_memory_retrieval_query(context, event)

    assert "最近对话" in query
    assert "五菱2026中秋" in query
    assert "当前消息" in query
    assert "不满意" in query
