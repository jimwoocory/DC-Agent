"""Tests for the 2026-06-11 fix that stops ``daily_response`` cards from
silently falling back to plain text when ``on_decorating_result`` re-fires.

Production symptom (data/card_runtime/events.jsonl):
    start   thinking_waiting  ok=true
    finalize daily_response   ok=true     ← 1st LLM turn: card OK
    finalize daily_response   ok=false    fallback=plain_text  ← 2nd turn, stream popped
    finalize daily_response   ok=false    fallback=plain_text  ← 3rd turn, stream popped

Two layers of defense are now in place:

1. ``feishu_card_streamer.streamer.FeishuCardStreamer.finalize`` is now
   idempotent: if the stream is already gone (i.e. a prior finalize ran
   its ``finally`` block and popped it), it returns ``True`` instead of
   ``False``. This stops the ``ok=false`` event from being recorded for
   benign re-fires.

2. ``daily_card_renderer.DailyCardRendererPlugin.finalize_or_render_card``
   keeps a per-instance ``_finalized_stream_ids`` set. Once a stream_id
   has been finalized (or short-circuited), subsequent invocations on
   the same stream_id consume the new ``result`` and return early —
   before any Feishu API call is made. This is the primary fix; the
   streamer change is defense in depth.

Both are tested here.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from astrbot.api.event import MessageEventResult, ResultContentType

# ─── paths ────────────────────────────────────────────────────────────────

_ROOT = Path(__file__).resolve().parents[2]
_DC_ENGINES_ROOT = _ROOT / "dc_engines"
_PLUGIN_PATH = _ROOT / "data" / "plugins" / "daily_card_renderer" / "main.py"
_STREAMER_PATH = (
    _DC_ENGINES_ROOT / "dc_engines" / "feishu_card_streamer" / "streamer.py"
)

if str(_DC_ENGINES_ROOT) not in sys.path:
    sys.path.insert(0, str(_DC_ENGINES_ROOT))


# ─── helpers ──────────────────────────────────────────────────────────────


def _load_daily_card_renderer():
    spec = importlib.util.spec_from_file_location(
        "daily_card_renderer_under_test", _PLUGIN_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_streamer_module():
    spec = importlib.util.spec_from_file_location(
        "feishu_card_streamer_under_test", _STREAMER_PATH
    )
    assert spec is not None and spec.loader is not None
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
    s = MagicMock()
    s.get_stream.return_value = SimpleNamespace(finalized=False)
    return s


def _make_plugin(renderer, streamer):
    plugin = object.__new__(renderer.DailyCardRendererPlugin)
    plugin.context = _make_context(streamer)
    plugin._finalized_stream_ids = {}
    return plugin


# ─── streamer-level idempotency ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_streamer_finalize_is_idempotent_when_stream_already_popped() -> None:
    """Return idempotent success when a previous finalize already popped the stream."""
    streamer_mod = _load_streamer_module()
    streamer = streamer_mod.FeishuCardStreamer.__new__(streamer_mod.FeishuCardStreamer)
    streamer._streams = {}
    streamer._finalized_message_ids = {}
    # No client mock is required because this path returns before patching.
    card = {"elements": []}

    assert await streamer.finalize("om_already_gone", card) is True
    assert "om_already_gone" not in streamer._streams
    assert "om_already_gone" in streamer._finalized_message_ids


@pytest.mark.asyncio
async def test_streamer_finalize_first_call_still_returns_true_with_real_stream() -> (
    None
):
    """The first finalize with a real stream still patches and returns success."""
    streamer_mod = _load_streamer_module()

    class _FakeResponse:
        def success(self) -> bool:
            return True

    class _FakeMessageApi:
        async def apatch(self, req):
            return _FakeResponse()

    class _FakeClient:
        class _V1:
            message = _FakeMessageApi()

        im = type("_Im", (), {"v1": _V1()})()

    streamer = streamer_mod.FeishuCardStreamer.__new__(streamer_mod.FeishuCardStreamer)
    streamer._client = _FakeClient()
    streamer._streams = {
        "om_first": SimpleNamespace(
            finalized=False,
            auto_update_task=None,
            last_card=None,
            elapsed_sec=1.0,
        )
    }
    streamer._finalized_message_ids = {}

    card = {"elements": [{"tag": "div", "text": {"content": "hi"}}]}
    ok = await streamer.finalize("om_first", card)
    assert ok is True
    assert "om_first" not in streamer._streams


@pytest.mark.asyncio
async def test_streamer_schedule_retract_deletes_message() -> None:
    """Retractable task cards should be deleted via Feishu message delete."""
    streamer_mod = _load_streamer_module()
    deleted_message_ids: list[str] = []

    class _FakeResponse:
        def success(self) -> bool:
            return True

    class _FakeMessageApi:
        async def adelete(self, req):
            deleted_message_ids.append(req.message_id)
            return _FakeResponse()

    class _FakeClient:
        class _V1:
            message = _FakeMessageApi()

        im = type("_Im", (), {"v1": _V1()})()

    streamer = streamer_mod.FeishuCardStreamer(_FakeClient())  # type: ignore[arg-type]
    streamer._streams = {
        "om_retract": SimpleNamespace(
            finalized=False,
            auto_update_task=None,
            last_card=None,
            elapsed_sec=1.0,
        )
    }

    task = streamer.schedule_retract("om_retract", delay_sec=0)
    assert task is not None
    await task

    assert deleted_message_ids == ["om_retract"]
    assert "om_retract" not in streamer._streams
    assert "om_retract" in streamer._finalized_message_ids


# ─── daily_card_renderer-level dedup ──────────────────────────────────────


@pytest.mark.asyncio
async def test_duplicate_finalize_on_same_stream_id_consumes_result_and_skips() -> None:
    """Duplicate finalizes for the same stream consume the result and skip patching."""
    renderer = _load_daily_card_renderer()
    streamer = _make_streamer()
    plugin = _make_plugin(renderer, streamer)

    finalize_mock = AsyncMock(return_value=True)
    monkeypatch_fn = getattr(renderer, "finalize_card_via_runtime", None)
    assert monkeypatch_fn is not None
    setattr(renderer, "finalize_card_via_runtime", finalize_mock)
    try:
        result_1 = (
            MessageEventResult()
            .message("## 第一段回复\n\n内容 A")
            .set_result_content_type(ResultContentType.LLM_RESULT)
        )
        event_1 = _make_lark_event(result_1, stream_id="om_dup_test")
        await plugin.finalize_or_render_card(event_1)
        assert finalize_mock.await_count == 1
        assert result_1.chain == []  # consumed
        assert "om_dup_test" in plugin._finalized_stream_ids

        result_2 = (
            MessageEventResult()
            .message("## 第二段回复\n\n内容 B (multi-turn 重入)")
            .set_result_content_type(ResultContentType.LLM_RESULT)
        )
        event_2 = _make_lark_event(result_2, stream_id="om_dup_test")
        await plugin.finalize_or_render_card(event_2)
        assert finalize_mock.await_count == 1, (
            "second finalize must not call finalize_card_via_runtime again"
        )
        assert result_2.chain == [], "second finalize must still consume the result"
    finally:
        setattr(renderer, "finalize_card_via_runtime", monkeypatch_fn)


@pytest.mark.asyncio
async def test_dedup_does_not_block_different_stream_ids() -> None:
    """Dedup is keyed by stream_id and does not block different messages."""
    renderer = _load_daily_card_renderer()
    streamer = _make_streamer()
    plugin = _make_plugin(renderer, streamer)

    finalize_mock = AsyncMock(return_value=True)
    setattr(renderer, "finalize_card_via_runtime", finalize_mock)
    try:
        for stream_id in ("om_1", "om_2", "om_3"):
            result = (
                MessageEventResult()
                .message(f"## reply for {stream_id}")
                .set_result_content_type(ResultContentType.LLM_RESULT)
            )
            event = _make_lark_event(result, stream_id=stream_id)
            await plugin.finalize_or_render_card(event)
        assert finalize_mock.await_count == 3
        assert set(plugin._finalized_stream_ids) == {"om_1", "om_2", "om_3"}
    finally:
        original = getattr(renderer, "_orig_finalize_for_test", None)
        if original is not None:
            setattr(renderer, "finalize_card_via_runtime", original)


@pytest.mark.asyncio
async def test_dedup_set_fifo_eviction_above_cap() -> None:
    """The plugin-level finalized set evicts the oldest stream at capacity."""
    renderer = _load_daily_card_renderer()
    streamer = _make_streamer()
    plugin = _make_plugin(renderer, streamer)

    finalize_mock = AsyncMock(return_value=True)
    setattr(renderer, "finalize_card_via_runtime", finalize_mock)
    cap = renderer.DailyCardRendererPlugin._FINALIZED_DEDUP_CAP
    try:
        for i in range(cap + 5):
            result = (
                MessageEventResult()
                .message(f"## reply {i}")
                .set_result_content_type(ResultContentType.LLM_RESULT)
            )
            event = _make_lark_event(result, stream_id=f"om_{i:05d}")
            await plugin.finalize_or_render_card(event)

        assert len(plugin._finalized_stream_ids) == cap
        for evicted in (f"om_{i:05d}" for i in range(5)):
            assert evicted not in plugin._finalized_stream_ids
        latest = next(reversed(plugin._finalized_stream_ids))
        assert latest == f"om_{cap + 4:05d}"
    finally:
        pass


# ─── integration: replay 6-11 production event pattern ────────────────────


@pytest.mark.asyncio
async def test_replay_6_11_production_event_pattern() -> None:
    """Replay the 2026-06-11 race from data/card_runtime/events.jsonl:
        1) waiting card start OK
        2) finalize OK (1st)
        3) finalize OK (2nd), previously recorded ok=false fallback=plain_text
        4) finalize OK (3rd), previously recorded ok=false fallback=plain_text

    After the fix, later finalizes pass idempotently and consume their results.
    """
    renderer = _load_daily_card_renderer()
    streamer = _make_streamer()
    plugin = _make_plugin(renderer, streamer)

    call_count = 0

    async def fake_finalize(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return True

    setattr(renderer, "finalize_card_via_runtime", fake_finalize)
    try:
        r1 = (
            MessageEventResult()
            .message("## 1st 完整回复")
            .set_result_content_type(ResultContentType.LLM_RESULT)
        )
        e1 = _make_lark_event(r1, stream_id="om_x100b6d90a90a1488b4b8abdd00ed1bb")
        await plugin.finalize_or_render_card(e1)
        assert call_count == 1
        assert r1.chain == []

        r2 = (
            MessageEventResult()
            .message("## 2nd 误以为是 final 的回复 (应该被 dedup)")
            .set_result_content_type(ResultContentType.LLM_RESULT)
        )
        e2 = _make_lark_event(r2, stream_id="om_x100b6d90a90a1488b4b8abdd00ed1bb")
        await plugin.finalize_or_render_card(e2)
        assert call_count == 1, "second finalize must dedupe and skip patching"
        assert r2.chain == [], "second finalize must consume the current result"

        r3 = (
            MessageEventResult()
            .message("## 3rd 又是误以为是 final")
            .set_result_content_type(ResultContentType.LLM_RESULT)
        )
        e3 = _make_lark_event(r3, stream_id="om_x100b6d90a90a1488b4b8abdd00ed1bb")
        await plugin.finalize_or_render_card(e3)
        assert call_count == 1
        assert r3.chain == []
    finally:
        pass


# ─── streamer-level dedup set for CLI paths ───────────────────────────────


@pytest.mark.asyncio
async def test_streamer_dedup_set_blocks_duplicate_patch_on_same_message_id() -> None:
    """Streamer-level dedup protects CLI paths that bypass the card plugin."""
    streamer_mod = _load_streamer_module()

    patch_call_count = 0

    class _FakeResponse:
        def success(self) -> bool:
            return True

    class _FakeMessageApi:
        async def apatch(self, req):
            nonlocal patch_call_count
            patch_call_count += 1
            return _FakeResponse()

    class _FakeClient:
        class _V1:
            message = _FakeMessageApi()

        im = type("_Im", (), {"v1": _V1()})()

    streamer = streamer_mod.FeishuCardStreamer(_FakeClient())  # type: ignore[arg-type]
    streamer._streams = {
        "om_cli_path": SimpleNamespace(
            finalized=False,
            auto_update_task=None,
            last_card=None,
            elapsed_sec=1.0,
        )
    }

    card = {"elements": []}

    ok1 = await streamer.finalize("om_cli_path", card)
    assert ok1 is True
    assert patch_call_count == 1
    assert "om_cli_path" in streamer._finalized_message_ids

    ok2 = await streamer.finalize("om_cli_path", card)
    assert ok2 is True
    assert patch_call_count == 1, (
        "second finalize should be short-circuited by _finalized_message_ids"
    )

    ok3 = await streamer.finalize("om_cli_path", card)
    assert ok3 is True
    assert patch_call_count == 1


def test_streamer_dedup_set_evicts_oldest_at_cap() -> None:
    """The streamer finalized set evicts the oldest message at capacity."""
    streamer_mod = _load_streamer_module()

    class _FakeClient:
        class _V1:
            message = None

        im = type("_Im", (), {"v1": _V1()})()

    streamer = streamer_mod.FeishuCardStreamer(_FakeClient())  # type: ignore[arg-type]

    cap = streamer_mod.FeishuCardStreamer._FINALIZED_DEDUP_CAP
    for i in range(cap):
        streamer._remember_finalized(f"om_old_{i:04d}")
    assert len(streamer._finalized_message_ids) == cap

    streamer._remember_finalized("om_new")
    assert len(streamer._finalized_message_ids) == cap
    assert "om_old_0000" not in streamer._finalized_message_ids
    assert "om_new" in streamer._finalized_message_ids
    assert "om_old_0001" in streamer._finalized_message_ids
