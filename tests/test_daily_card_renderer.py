from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlsplit

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


def _make_lark_event(
    result: MessageEventResult,
    *,
    stream_id: str | None = None,
    extra_overrides: dict[str, object] | None = None,
):
    extras = {
        "_daily_card_thinking_stream_id": stream_id,
        "dc_router_intent": "casual",
    }
    extras.update(extra_overrides or {})
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


def _make_webchat_event(result: MessageEventResult):
    event = _make_lark_event(result)
    event.get_platform_id.return_value = "webchat"
    event.get_platform_name.return_value = "webchat"
    event.message_obj.raw_message = SimpleNamespace(chat_id="")
    event.get_group_id.return_value = ""
    event.get_sender_id.return_value = "ou_smoke_user_1"
    return event


def _make_context(streamer):
    ctx = MagicMock()
    ctx.feishu_streamers = {"巅池-Agent小助手": streamer}
    ctx.get_platform_inst.return_value = SimpleNamespace(config={"app_id": "cli_test"})
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
async def test_workbench_copy_result_card_has_material_completion_entry(
    monkeypatch,
) -> None:
    renderer = _load_daily_card_renderer()
    streamer = _make_streamer()
    plugin = object.__new__(renderer.DailyCardRendererPlugin)
    plugin.context = _make_context(streamer)
    plugin._finalized_stream_ids = {}
    finalize = AsyncMock(return_value=True)
    monkeypatch.setattr(renderer, "finalize_card_via_runtime", finalize)

    result = (
        MessageEventResult()
        .message("## 新品文案\n\n夏日清爽上市，欢迎到店体验。")
        .set_result_content_type(ResultContentType.LLM_RESULT)
    )
    workspace_url = "http://127.0.0.1:6185/api/v1/assistant-attachments/copy-token"
    event = _make_lark_event(
        result,
        stream_id="om_copy_waiting",
        extra_overrides={
            "assistant_workbench_task_type": "copy",
            "assistant_workbench_workspace_url": workspace_url,
        },
    )

    await plugin.finalize_or_render_card(event)

    card = finalize.await_args.kwargs["card"]
    buttons = [
        element
        for element in card["body"]["elements"]
        if element.get("tag") == "button"
    ]
    assert [button["text"]["content"] for button in buttons] == ["补齐资料"]
    app_link = buttons[0]["behaviors"][0]["pc_url"]
    app_link_query = parse_qs(urlsplit(app_link).query)
    assert app_link_query["reload"] == ["true"]
    revision_url = app_link_query["lk_target_url"][0]
    assert revision_url.startswith(workspace_url)
    assert parse_qs(urlsplit(revision_url).query)["mode"] == ["revise"]


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


@pytest.mark.asyncio
async def test_webchat_result_does_not_send_feishu_card(monkeypatch) -> None:
    renderer = _load_daily_card_renderer()
    streamer = _make_streamer()
    plugin = object.__new__(renderer.DailyCardRendererPlugin)
    plugin.context = _make_context(streamer)
    plugin._finalized_stream_ids = {}
    send_card = AsyncMock(return_value=SimpleNamespace(message_id="om_should_not_send"))
    monkeypatch.setattr(renderer, "send_card_via_runtime", send_card)

    result = (
        MessageEventResult()
        .message("收到，我会按一句话回复。")
        .set_result_content_type(ResultContentType.LLM_RESULT)
    )
    event = _make_webchat_event(result)

    await plugin.finalize_or_render_card(event)

    send_card.assert_not_awaited()
    assert result.chain
    assert result.result_content_type == ResultContentType.LLM_RESULT


@pytest.mark.asyncio
async def test_webchat_waiting_card_does_not_send_feishu_card(monkeypatch) -> None:
    renderer = _load_daily_card_renderer()
    streamer = _make_streamer()
    plugin = object.__new__(renderer.DailyCardRendererPlugin)
    plugin.context = _make_context(streamer)
    send_card = AsyncMock(return_value=SimpleNamespace(message_id="om_should_not_send"))
    monkeypatch.setattr(renderer, "send_card_via_runtime", send_card)

    result = MessageEventResult().message("稍等，我处理一下。")
    event = _make_webchat_event(result)

    await plugin._start_thinking_card_if_needed(event)

    send_card.assert_not_awaited()


@pytest.mark.asyncio
async def test_media_route_result_does_not_render_second_casual_card(
    monkeypatch,
) -> None:
    renderer = _load_daily_card_renderer()
    streamer = _make_streamer()
    plugin = object.__new__(renderer.DailyCardRendererPlugin)
    plugin.context = _make_context(streamer)
    plugin._finalized_stream_ids = {}
    send_card = AsyncMock(return_value=SimpleNamespace(message_id="om_duplicate"))
    monkeypatch.setattr(renderer, "send_card_via_runtime", send_card)

    result = (
        MessageEventResult()
        .message("已进入生图任务：GPT Image 2 主用，Dreamina 即梦自动兜底。")
        .set_result_content_type(ResultContentType.GENERAL_RESULT)
    )
    event = _make_lark_event(result)
    original_get_extra = event.get_extra
    event.get_extra.side_effect = lambda key: (
        "image" if key == "dc_media_route_handled" else original_get_extra(key)
    )

    await plugin.finalize_or_render_card(event)

    send_card.assert_not_awaited()
    assert result.chain


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
