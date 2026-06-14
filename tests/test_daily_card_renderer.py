from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from astrbot.api.event import MessageEventResult, ResultContentType

_ROOT = Path(__file__).resolve().parents[1]
_DC_ENGINES_ROOT = _ROOT / "dc_engines"
_PLUGIN_PATH = _ROOT / "data" / "plugins" / "daily_card_renderer" / "main.py"

if str(_DC_ENGINES_ROOT) not in sys.path:
    sys.path.insert(0, str(_DC_ENGINES_ROOT))


def _load_daily_card_renderer():
    spec = importlib.util.spec_from_file_location(
        "daily_card_renderer_under_test",
        _PLUGIN_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _make_lark_event(result: MessageEventResult, *, stream_id: str | None = None):
    extras = {
        "_daily_card_thinking_stream_id": stream_id,
        "dc_router_intent": "casual",
    }
    event = MagicMock()
    event.get_platform_id.return_value = "巅池-Agent小助手"
    event.get_platform_name.return_value = "lark"
    event.get_result.return_value = result
    event.get_extra.side_effect = lambda key: extras.get(key)
    event.set_extra.side_effect = lambda key, value: extras.__setitem__(key, value)
    event.message_str = "你好"
    event.message_obj = MagicMock()
    event.message_obj.raw_message = SimpleNamespace(chat_id="oc_test")
    event.get_group_id.return_value = "oc_test"
    event.get_sender_id.return_value = "ou_test"
    return event


def _make_context(streamer):
    ctx = MagicMock()
    ctx.feishu_streamers = {"巅池-Agent小助手": streamer}
    return ctx


def _make_streamer():
    streamer = MagicMock()
    streamer.get_stream.return_value = SimpleNamespace(finalized=False)
    return streamer


@pytest.mark.asyncio
async def test_waiting_card_finalize_consumes_llm_result(monkeypatch) -> None:
    renderer = _load_daily_card_renderer()
    streamer = _make_streamer()
    plugin = object.__new__(renderer.DailyCardRendererPlugin)
    plugin.context = _make_context(streamer)
    plugin._finalized_stream_ids = {}
    monkeypatch.setattr(
        renderer, "finalize_card_via_runtime", AsyncMock(return_value=True)
    )

    result = (
        MessageEventResult()
        .message("## 柳州天气提醒\n\n路面积水，出门记得带伞。")
        .set_result_content_type(ResultContentType.LLM_RESULT)
    )
    event = _make_lark_event(result, stream_id="om_waiting")

    await plugin.finalize_or_render_card(event)

    assert result.chain == []
    assert result.result_content_type == ResultContentType.GENERAL_RESULT


@pytest.mark.asyncio
async def test_short_card_render_consumes_llm_result(monkeypatch) -> None:
    renderer = _load_daily_card_renderer()
    streamer = _make_streamer()
    plugin = object.__new__(renderer.DailyCardRendererPlugin)
    plugin.context = _make_context(streamer)
    plugin._finalized_stream_ids = {}
    monkeypatch.setattr(
        renderer,
        "send_card_via_runtime",
        AsyncMock(return_value=SimpleNamespace(message_id="om_short")),
    )

    result = (
        MessageEventResult()
        .message("是啊，柳州这几天雨势确实很猛，出门注意安全。")
        .set_result_content_type(ResultContentType.LLM_RESULT)
    )
    event = _make_lark_event(result)

    await plugin.finalize_or_render_card(event)

    assert result.chain == []
    assert result.result_content_type == ResultContentType.GENERAL_RESULT


def test_consumed_card_result_will_not_trigger_empty_model_fallback() -> None:
    renderer = _load_daily_card_renderer()
    result = (
        MessageEventResult()
        .message("")
        .set_result_content_type(ResultContentType.LLM_RESULT)
    )
    assert result.is_model_result() is True

    renderer._consume_rendered_result(result)

    assert result.chain == []
    assert result.is_model_result() is False
