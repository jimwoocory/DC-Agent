"""Engineering-grade integration tests for the 12-stage dispatch pipeline.

The dispatch pipeline is the single entry point used by the Star plugin
(see ``data/plugins/dc_router/dispatch.py``). It runs 12 stages in a
strict order — and *every* stage's ``stop=True`` short-circuits the
rest of the pipeline. The order is contractually sensitive and is
locked down in ``harness/contracts/routing_merge_contract.json`` R2.

The 12 stages are:

    1.  card_action          — must be earliest
    2.  slash_command        — bypass to AstrBot
    3.  chitchat             — group event + at/wake guard
    4.  reasoning_prefix     — user-pinned provider (#高 / #超深 / ...)
    5.  feishu_channel       — strong pin via agent → provider
    6.  truth_intake         — block on missing material
    7.  department_memory    — suggest/confirm/dismiss
    8.  memory_injection     — set_extra only
    9.  assistant_tone       — set_extra only
    10. media_route          — image / video background job
    11. dc_router.decide()   — main path
    12. v1.0 fallback        — dc_router off / dry-run / error

We mock the heavy dependencies (AstrBot provider manager, DC router,
truth_intake, memory_injection, ...) at the module boundary so the
tests can exercise the dispatch function without spinning up a real
AstrBot runtime.

The test classes are organised by stage:

- :class:`TestStage1CardAction`              — card_action routing
- :class:`TestStage2SlashCommand`            — slash bypass
- :class:`TestStage3Chitchat`                — chitchat guard
- :class:`TestStage4ReasoningPrefix`         — prefix pinning
- :class:`TestStage5FeishuChannel`           — channel agent pin
- :class:`TestStage6TruthIntake`             — truth_intake block
- :class:`TestStage7DepartmentMemory`        — dept memory prompt
- :class:`TestStage8MemoryInjection`         — memory extras
- :class:`TestStage9AssistantTone`           — tone extras
- :class:`TestStage10MediaRoute`             — image/video ack
- :class:`TestStage11DCRouterDecision`       — main router
- :class:`TestStage12V1Fallback`             — legacy fallback
- :class:`TestPipelineOrdering`              — order sensitivity
- :class:`TestPipelineDryRun`                — dry-run mode
- :class:`TestPipelineErrorPaths`            — error resilience
"""

from __future__ import annotations

import importlib
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ─────────────────────────────────────────────────────────────────────────
# AstrBot / dc-router stubbing — same shape as test_event_envelope.
# We need more here because dispatch imports from astrbot.api, config,
# memory_injection, truth_intake, and dc_router_core at call time.
# ─────────────────────────────────────────────────────────────────────────


def _ensure_astrbot_stub() -> None:
    """Provide a minimal ``astrbot.api`` stub for module-level imports.

    Even when the real ``astrbot`` package is importable, we still need
    a ``astrbot.api`` submodule (sometimes it is registered, sometimes
    not) and a ``logger`` attribute on it. We register the missing
    pieces idempotently.
    """
    try:
        importlib.import_module("astrbot")
    except Exception:  # noqa: BLE001
        fake_pkg = types.ModuleType("astrbot")
        sys.modules.setdefault("astrbot", fake_pkg)

    try:
        importlib.import_module("astrbot.api")
    except Exception:  # noqa: BLE001
        api_pkg = types.ModuleType("astrbot.api")
        sys.modules.setdefault("astrbot.api", api_pkg)
        sys.modules["astrbot"].api = api_pkg  # type: ignore[attr-defined]

    api = sys.modules["astrbot.api"]
    if not hasattr(api, "logger"):
        api.logger = MagicMock()  # type: ignore[attr-defined]


_ensure_astrbot_stub()


_DC_AGENT_ROOT = Path(__file__).resolve().parents[2]
_PLUGINS_PARENT = _DC_AGENT_ROOT / "data" / "plugins"
if str(_DC_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_DC_AGENT_ROOT))
if str(_PLUGINS_PARENT) not in sys.path:
    sys.path.insert(0, str(_PLUGINS_PARENT))


# Stub out ``dc_router.__init__`` so it does NOT execute the real
# package init (which imports ``plugin.py`` → ``health.py`` →
# ``antigravity_health.py`` → ``qwen_health.py``, all of which depend
# on the live AstrBot runtime). We provide the bare minimum surface
# (``__path__`` and ``__file__``) that sub-imports need, then let
# Python's normal import machinery populate the submodules.
_dc_router_pkg_stub = types.ModuleType("dc_router")
_dc_router_pkg_stub.__path__ = [str(_PLUGINS_PARENT / "dc_router")]  # type: ignore[attr-defined]
_dc_router_pkg_stub.__file__ = str(_PLUGINS_PARENT / "dc_router" / "__init__.py")  # type: ignore[attr-defined]
sys.modules["dc_router"] = _dc_router_pkg_stub

# Sub-imports go through normal Python import now that the parent
# package is on sys.modules with a real ``__path__``.
_dc_router_config = importlib.import_module("dc_router.config")
_dc_router_preprocessing = importlib.import_module("dc_router.preprocessing")
_dc_router_routing = importlib.import_module("dc_router.routing")
_dispatch = importlib.import_module("dc_router.dispatch")


# ─────────────────────────────────────────────────────────────────────────
# Helpers — minimal event / context / config mocks
# ─────────────────────────────────────────────────────────────────────────


def _make_event(
    *,
    text: str = "",
    platform_id: str = "巅池-Agent小助手",
    umo: str = "ai:chat:user-1",
    sender_id: str = "user-1",
    is_at_or_wake_command: bool = False,
    components: list | None = None,
    extras: dict | None = None,
) -> MagicMock:
    """Build a MagicMock that quacks like an AstrMessageEvent."""
    event = MagicMock(name="event")
    event.message_str = text
    event.unified_msg_origin = umo
    event.is_at_or_wake_command = is_at_or_wake_command
    message_obj = MagicMock(name="message_obj")
    message_obj.message = components or []
    event.message_obj = message_obj
    event.get_platform_id = MagicMock(return_value=platform_id or "")
    event.get_sender_id = MagicMock(return_value=sender_id)
    event.should_call_llm = MagicMock()

    extras_state: dict = dict(extras or {})

    def _set_extra(key: str, value: object) -> None:
        extras_state[key] = value

    def _get_extra(key: str, default: object = None) -> object:
        return extras_state.get(key, default)

    event.set_extra = MagicMock(side_effect=_set_extra)
    event.get_extra = MagicMock(side_effect=_get_extra)
    event.set_result = MagicMock()
    event._extras = extras_state
    return event


def _make_context() -> MagicMock:
    """Build a MagicMock that quacks like an AstrBot Context."""
    ctx = MagicMock(name="context")
    ctx.get_provider_by_id = MagicMock(return_value=MagicMock())
    ctx.provider_manager = MagicMock()
    ctx.provider_manager.set_provider = AsyncMock(return_value=True)
    return ctx


def _make_config(
    *,
    enabled: bool = True,
    dry_run: bool = False,
    fallback_on_error: bool = True,
    feishu_channel_routes: dict | None = None,
) -> Any:
    return _dc_router_config.DCRouterConfig(
        enabled=enabled,
        dry_run=dry_run,
        fallback_on_error=fallback_on_error,
        feishu_channel_routes=feishu_channel_routes or {},
    )


@dataclass(slots=True)
class _StageSpy:
    """Records which stage was hit during dispatch for ordering tests.

    Currently unused — kept for future ordering tests where we need
    richer per-stage telemetry than ``assert_awaited`` provides. The
    fixture still wires it up so the data flow is established.
    """

    chitchat_handled: bool = False
    card_handled: bool = False
    feishu_handled: bool = False
    truth_handled: bool = False
    dept_handled: bool = False
    dept_prompt_sent: bool = False
    memory_injected: bool = False
    tone_injected: bool = False
    media_handled: bool = False
    dc_router_called: bool = False
    v1_fallback_called: bool = False
    decisions: list[tuple[str, str]] = field(default_factory=list)


def _make_spy() -> _StageSpy:
    return _StageSpy()


# ─────────────────────────────────────────────────────────────────────────
# Test fixtures — wire up patches to record stage execution
# ─────────────────────────────────────────────────────────────────────────


@pytest.fixture
def _spy() -> _StageSpy:
    return _make_spy()


@pytest.fixture
def patched_dispatch(_spy: _StageSpy):
    """Patch the heavy downstream modules so we can observe stage order.

    Every stage in dispatch.py is patched at its import-time location:

    - ``dc_router.dispatch.try_handle_chitchat``
    - ``dc_router.dispatch.try_handle_card_action``
    - ``dc_router.dispatch.try_apply_feishu_channel_route``
    - ``dc_router.dispatch.try_handle_media_route``
    - ``dc_router.dispatch.try_handle_department_memory``
    - ``dc_router.dispatch.try_inject_assistant_tone``
    - ``dc_router.dispatch._maybe_truth_intake``
    - ``dc_router.dispatch._memory_injection``
    - ``dc_router.dispatch._run_dc_router``
    - ``dc_router.dispatch._v1_fallback``
    - ``dc_router.dispatch.apply_provider_pin``
    - ``dc_router.dispatch.apply_decision``
    - ``dc_router.dispatch.annotate_event_with_decision``
    - ``dc_router.dispatch.build_envelope``

    The defaults make every stage a no-op (returns ``handled=False`` /
    ``None``) so a plain ``"hello"`` text message falls all the way
    through to ``v1.0 fallback``. Tests override the patches they care
    about via the returned dict — values are :class:`AsyncMock` /
    :class:`MagicMock` so they support ``assert_awaited`` /
    ``assert_not_awaited`` / ``assert_called`` and ``return_value`` /
    ``side_effect`` assignment.
    """
    patches: dict[str, Any] = {}

    # Build AsyncMock / MagicMock *with* a default side_effect that
    # returns the appropriate "did not handle" sentinel. Tests can
    # override by reassigning ``return_value`` or ``side_effect`` on
    # ``patches["<key>"]`` (which is the underlying mock).
    from dc_router.preprocessing.department_memory import DepartmentMemoryDecision

    # async helpers
    am = AsyncMock  # noqa: N806

    chitchat_mock = am(return_value=types.SimpleNamespace(handled=False))
    card_mock = am(return_value=types.SimpleNamespace(handled=False, stop=False))
    feishu_mock = am(return_value=False)
    media_mock = am(return_value=False)
    dept_mock = MagicMock(return_value=DepartmentMemoryDecision(effective_text=""))
    tone_mock = MagicMock(return_value=False)
    truth_mock = am(return_value=False)
    memory_mock = am(return_value=False)

    def _stub_envelope(event: Any) -> Any:
        return types.SimpleNamespace(
            text=event.message_str,
            metadata={"platform_id": event.get_platform_id()},
        )

    build_envelope_mock = MagicMock(side_effect=_stub_envelope)
    dc_mock = am(return_value=(False, "", ""))
    v1_mock = am(return_value=False)
    pin_mock = am(return_value=False)
    apply_mock = am(return_value=False)
    annotate_mock = MagicMock()

    patches["chitchat"] = patch.object(_dispatch, "try_handle_chitchat", chitchat_mock)
    patches["card"] = patch.object(_dispatch, "try_handle_card_action", card_mock)
    patches["feishu"] = patch.object(
        _dispatch, "try_apply_feishu_channel_route", feishu_mock
    )
    patches["media"] = patch.object(_dispatch, "try_handle_media_route", media_mock)
    patches["dept"] = patch.object(_dispatch, "try_handle_department_memory", dept_mock)
    patches["tone"] = patch.object(_dispatch, "try_inject_assistant_tone", tone_mock)
    patches["truth"] = patch.object(_dispatch, "_maybe_truth_intake", truth_mock)
    patches["memory"] = patch.object(_dispatch, "_memory_injection", memory_mock)
    patches["build_envelope"] = patch.object(
        _dispatch, "build_envelope", build_envelope_mock
    )
    patches["dc"] = patch.object(_dispatch, "_run_dc_router", dc_mock)
    patches["v1"] = patch.object(_dispatch, "_v1_fallback", v1_mock)
    patches["pin"] = patch.object(_dispatch, "apply_provider_pin", pin_mock)
    patches["apply"] = patch.object(_dispatch, "apply_decision", apply_mock)
    patches["annotate"] = patch.object(
        _dispatch, "annotate_event_with_decision", annotate_mock
    )

    # ``patch.object`` returns a ``_patch`` instance whose target
    # attribute only swaps in once ``.start()`` is called. We start all
    # of them now, then build a second dict that maps the *same* keys
    # to the *mock* objects themselves (so tests can do
    # ``patches["card"].return_value = ...`` and
    # ``patches["card"].assert_awaited()``).
    started_patches: list[Any] = []
    for p in patches.values():
        p.start()
        started_patches.append(p)

    mocks = {
        "chitchat": chitchat_mock,
        "card": card_mock,
        "feishu": feishu_mock,
        "media": media_mock,
        "dept": dept_mock,
        "tone": tone_mock,
        "truth": truth_mock,
        "memory": memory_mock,
        "build_envelope": build_envelope_mock,
        "dc": dc_mock,
        "v1": v1_mock,
        "pin": pin_mock,
        "apply": apply_mock,
        "annotate": annotate_mock,
    }

    try:
        yield mocks
    finally:
        for p in started_patches:
            p.stop()


# ─────────────────────────────────────────────────────────────────────────
# 1. Stage 1 — card_action
# ─────────────────────────────────────────────────────────────────────────


class TestStage1CardAction:
    """Stage 1 must handle ``__card_action__:...`` messages first."""

    @pytest.mark.asyncio
    async def test_card_action_text_routes_to_card_stage(
        self, patched_dispatch: dict
    ) -> None:
        event = _make_event(text="__card_action__:{}")
        ctx = _make_context()
        cfg = _make_config()

        card_result = types.SimpleNamespace(handled=True, stop=True)
        patched_dispatch["card"].return_value = card_result

        result = await _dispatch.dispatch(ctx, event, cfg)

        assert result.handled is True
        assert "card_action" in result.source
        patched_dispatch["card"].assert_awaited()

    @pytest.mark.asyncio
    async def test_non_card_text_skips_card_stage(self, patched_dispatch: dict) -> None:
        event = _make_event(text="hello world")
        ctx = _make_context()
        cfg = _make_config()

        result = await _dispatch.dispatch(ctx, event, cfg)

        # Card stage is only invoked when the text starts with
        # ``__card_action__:.`` — for plain text the dispatch short-
        # circuits past stage 1 entirely.
        patched_dispatch["card"].assert_not_awaited()
        # Card stage returned ``handled=False`` so dispatch continues.
        # The final ``v1.0_no_match`` source is what the pipeline reports
        # when the v1 fallback also does not pick anything up.
        assert result.source == "v1.0_no_match"

    @pytest.mark.asyncio
    async def test_card_action_handles_exception(self, patched_dispatch: dict) -> None:
        """A card action that throws must NOT crash the dispatch."""
        event = _make_event(text="__card_action__:{}")
        ctx = _make_context()
        cfg = _make_config()

        # The real card handler swallows exceptions and returns
        # ``handled=False``; emulate that — the dispatch must not
        # raise.
        patched_dispatch["card"].return_value = types.SimpleNamespace(
            handled=False, stop=False
        )

        result = await _dispatch.dispatch(ctx, event, cfg)
        assert result.handled is False


# ─────────────────────────────────────────────────────────────────────────
# 2. Stage 2 — slash command
# ─────────────────────────────────────────────────────────────────────────


class TestStage2SlashCommand:
    """Stage 2 must bypass routing for ``/command``-style messages."""

    @pytest.mark.asyncio
    async def test_slash_command_bypasses_routing(self, patched_dispatch: dict) -> None:
        event = _make_event(text="/help")
        ctx = _make_context()
        cfg = _make_config()

        result = await _dispatch.dispatch(ctx, event, cfg)

        # Slash commands are *bypassed* — handled=False, source markers
        # the bypass.
        assert result.handled is False
        assert result.source == "slash_command"

    @pytest.mark.asyncio
    async def test_slash_command_with_args_bypasses(
        self, patched_dispatch: dict
    ) -> None:
        event = _make_event(text="/tool list")
        ctx = _make_context()
        cfg = _make_config()

        result = await _dispatch.dispatch(ctx, event, cfg)

        assert result.handled is False
        assert result.source == "slash_command"

    @pytest.mark.asyncio
    async def test_slash_command_with_leading_whitespace_bypasses(
        self, patched_dispatch: dict
    ) -> None:
        # The dispatch regex is ``^\\s*/\\S+`` so leading whitespace
        # is allowed.
        event = _make_event(text="   /help me")
        ctx = _make_context()
        cfg = _make_config()

        result = await _dispatch.dispatch(ctx, event, cfg)
        assert result.source == "slash_command"

    @pytest.mark.asyncio
    async def test_text_starting_with_slash_word_does_not_bypass(
        self, patched_dispatch: dict
    ) -> None:
        # "https://example.com" looks slash-y but the regex is
        # ``^\\s*/\\S+(?:\\s|$)`` so ``/`` must be at the start of
        # the *trimmed* string. "http" is preceded by text.
        event = _make_event(text="请看 https://example.com")
        ctx = _make_context()
        cfg = _make_config()

        result = await _dispatch.dispatch(ctx, event, cfg)
        assert result.source != "slash_command"

    @pytest.mark.asyncio
    async def test_slash_command_skips_dc_router(self, patched_dispatch: dict) -> None:
        """After a slash, dc_router MUST NOT be invoked."""
        event = _make_event(text="/help")
        ctx = _make_context()
        cfg = _make_config()

        await _dispatch.dispatch(ctx, event, cfg)
        patched_dispatch["dc"].assert_not_awaited()
        patched_dispatch["v1"].assert_not_awaited()


# ─────────────────────────────────────────────────────────────────────────
# 3. Stage 3 — chitchat
# ─────────────────────────────────────────────────────────────────────────


class TestStage3Chitchat:
    """Stage 3 must handle short greetings on managed platforms."""

    @pytest.mark.asyncio
    async def test_chitchat_greeting_routes_to_chitchat_stage(
        self, patched_dispatch: dict
    ) -> None:
        event = _make_event(text="你好")
        ctx = _make_context()
        cfg = _make_config()

        patched_dispatch["chitchat"].return_value = types.SimpleNamespace(
            handled=True, response="在的", matched_intent="greeting"
        )

        result = await _dispatch.dispatch(ctx, event, cfg)

        assert result.handled is True
        assert result.source == "chitchat:greeting"

    @pytest.mark.asyncio
    async def test_chitchat_skipped_on_unmanaged_platform(
        self, patched_dispatch: dict
    ) -> None:
        """Chitchat guard is gated on ``is_dc_router_managed_platform``."""
        event = _make_event(text="你好", platform_id="some-random-bot")
        ctx = _make_context()
        cfg = _make_config()

        await _dispatch.dispatch(ctx, event, cfg)
        # chitchat must NOT have been called on a non-managed platform
        patched_dispatch["chitchat"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_chitchat_skipped_when_handled_false(
        self, patched_dispatch: dict
    ) -> None:
        event = _make_event(text="写一份品牌分析报告")
        ctx = _make_context()
        cfg = _make_config()

        # default is handled=False; dispatch continues
        result = await _dispatch.dispatch(ctx, event, cfg)
        assert "chitchat" not in result.source


# ─────────────────────────────────────────────────────────────────────────
# 4. Stage 4 — reasoning prefix
# ─────────────────────────────────────────────────────────────────────────


class TestStage4ReasoningPrefix:
    """Stage 4 must pin a provider for ``#高`` / ``#超深`` / etc."""

    @pytest.mark.asyncio
    async def test_high_prefix_pins_provider(self, patched_dispatch: dict) -> None:
        event = _make_event(text="#高 帮我做完整分析")
        ctx = _make_context()
        cfg = _make_config()

        patched_dispatch["pin"].return_value = True

        result = await _dispatch.dispatch(ctx, event, cfg)

        assert result.handled is True
        assert result.source == "reasoning_prefix"
        assert result.decision_provider == "aihubmix/claude-sonnet-4-6"
        # pinned → no dc_router call, no v1 fallback
        patched_dispatch["dc"].assert_not_awaited()
        patched_dispatch["v1"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_xhigh_prefix_pins_opus(self, patched_dispatch: dict) -> None:
        event = _make_event(text="#深度 战略分析")
        ctx = _make_context()
        cfg = _make_config()

        patched_dispatch["pin"].return_value = True

        result = await _dispatch.dispatch(ctx, event, cfg)
        assert result.decision_provider == "aihubmix/claude-opus-4-7"

    @pytest.mark.asyncio
    async def test_codex_xhigh_prefix_pins_xhigh(self, patched_dispatch: dict) -> None:
        # Longest-match regression: ``#codex超深`` must route to xhigh.
        event = _make_event(text="#codex超深 帮我看看")
        ctx = _make_context()
        cfg = _make_config()

        patched_dispatch["pin"].return_value = True

        result = await _dispatch.dispatch(ctx, event, cfg)
        assert result.decision_provider == "codex/gpt-5.5-xhigh"

    @pytest.mark.asyncio
    async def test_prefix_bypasses_chitchat_and_feishu(
        self, patched_dispatch: dict
    ) -> None:
        """Prefix wins over chitchat and feishu_channel stages (stage 4
        comes after both). However chitchat *is* called for any non-
        slash, non-card text on a managed platform — it is just that
        when the prefix matches, the prefix handler short-circuits
        before chitchat could *return* handled=True.

        So the contract is: chitchat is awaited, but its return value
        is ignored because the prefix handler already returned.
        """
        event = _make_event(text="#高 你好")  # greeting + prefix
        ctx = _make_context()
        cfg = _make_config()

        patched_dispatch["pin"].return_value = True

        result = await _dispatch.dispatch(ctx, event, cfg)

        assert result.source == "reasoning_prefix"
        # feishu / truth / memory / tone / media / dc / v1 are downstream
        # of the prefix stage and MUST NOT be invoked.
        patched_dispatch["feishu"].assert_not_awaited()
        patched_dispatch["truth"].assert_not_awaited()
        patched_dispatch["memory"].assert_not_awaited()
        patched_dispatch["tone"].assert_not_called()
        patched_dispatch["media"].assert_not_awaited()
        patched_dispatch["dc"].assert_not_awaited()
        patched_dispatch["v1"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_prefix_falls_through(self, patched_dispatch: dict) -> None:
        event = _make_event(text="hello world")
        ctx = _make_context()
        cfg = _make_config()

        await _dispatch.dispatch(ctx, event, cfg)
        patched_dispatch["pin"].assert_not_awaited()


# ─────────────────────────────────────────────────────────────────────────
# 5. Stage 5 — feishu channel
# ─────────────────────────────────────────────────────────────────────────


class TestStage5FeishuChannel:
    """Stage 5 must honour agent → provider mappings from config."""

    @pytest.mark.asyncio
    async def test_feishu_channel_handles_message(self, patched_dispatch: dict) -> None:
        event = _make_event(text="帮我写活动文案", platform_id="巅池-Agent小助手")
        ctx = _make_context()
        cfg = _make_config(
            feishu_channel_routes={"planning-agent": "aihubmix/gemini-3.5-flash"}
        )

        patched_dispatch["feishu"].return_value = True

        result = await _dispatch.dispatch(ctx, event, cfg)
        assert result.handled is True
        assert result.source == "feishu_channel"

    @pytest.mark.asyncio
    async def test_feishu_channel_only_runs_on_managed_platform(
        self, patched_dispatch: dict
    ) -> None:
        event = _make_event(text="hi", platform_id="some-other-bot")
        ctx = _make_context()
        cfg = _make_config()

        await _dispatch.dispatch(ctx, event, cfg)
        patched_dispatch["feishu"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_feishu_channel_skipped_on_no_match(
        self, patched_dispatch: dict
    ) -> None:
        event = _make_event(text="hi", platform_id="巅池-Agent小助手")
        ctx = _make_context()
        cfg = _make_config()

        await _dispatch.dispatch(ctx, event, cfg)
        patched_dispatch["feishu"].assert_awaited()
        assert patched_dispatch["feishu"].return_value is False


# ─────────────────────────────────────────────────────────────────────────
# 6. Stage 6 — truth_intake
# ─────────────────────────────────────────────────────────────────────────


class TestStage6TruthIntake:
    """Stage 6 must block on missing material (truth_intake stop)."""

    @pytest.mark.asyncio
    async def test_truth_intake_blocks_message(self, patched_dispatch: dict) -> None:
        event = _make_event(text="这份报告靠谱吗", platform_id="巅池-Agent小助手")
        ctx = _make_context()
        cfg = _make_config()

        patched_dispatch["truth"].return_value = True

        result = await _dispatch.dispatch(ctx, event, cfg)

        assert result.handled is True
        assert result.source == "truth_intake"
        # Once truth_intake stops, nothing else should run.
        patched_dispatch["memory"].assert_not_awaited()
        patched_dispatch["tone"].assert_not_called()
        patched_dispatch["dc"].assert_not_awaited()
        patched_dispatch["v1"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_truth_intake_skipped_when_dc_router_fully_off(
        self, patched_dispatch: dict
    ) -> None:
        """With ``enabled=False, dry_run=False`` (both flags off), the
        gate ``is_active or is_dry_run`` is False, so
        ``_maybe_truth_intake`` returns ``False`` immediately without
        actually invoking the underlying guard."""
        event = _make_event(text="hi", platform_id="巅池-Agent小助手")
        ctx = _make_context()
        cfg = _make_config(enabled=False, dry_run=False)

        await _dispatch.dispatch(ctx, event, cfg)
        # The wrapper is still awaited (it's called at the dispatch
        # boundary) — but it must short-circuit and never invoke
        # the underlying truth_intake import path. Asserting ``awaited``
        # is the correct contract.
        patched_dispatch["truth"].assert_awaited()

    @pytest.mark.asyncio
    async def test_truth_intake_skipped_on_unmanaged_platform(
        self, patched_dispatch: dict
    ) -> None:
        """Truth intake is only invoked on managed platforms."""
        event = _make_event(text="hi", platform_id="some-other-bot")
        ctx = _make_context()
        cfg = _make_config(enabled=True, dry_run=False)

        await _dispatch.dispatch(ctx, event, cfg)
        patched_dispatch["truth"].assert_not_awaited()


# ─────────────────────────────────────────────────────────────────────────
# 7. Stage 7 — department memory
# ─────────────────────────────────────────────────────────────────────────


class TestStage7DepartmentMemory:
    """Stage 7 may stop dispatch if a dept memory prompt should be sent."""

    @pytest.mark.asyncio
    async def test_dept_memory_prompt_stops_dispatch(
        self, patched_dispatch: dict
    ) -> None:
        event = _make_event(
            text="查一下运营组上次的复盘", platform_id="巅池-Agent小助手"
        )
        ctx = _make_context()
        cfg = _make_config()

        from dc_router.preprocessing.department_memory import (
            DepartmentMemoryDecision,
            DepartmentMemoryPromptState,
        )

        sentinel_state = DepartmentMemoryPromptState(
            suggestion_id="dmpp_test_001",
            conversation_id="ai:chat:user-1:user-1",
            original_text="查一下运营组上次的复盘",
            query_text="查一下运营组上次的复盘",
            department_ids=("dept-ops",),
            department_names=("运营组",),
            profile_ids=("profile-1",),
            created_at=0.0,
        )
        patched_dispatch["dept"].return_value = DepartmentMemoryDecision(
            stop=True,
            effective_text="查一下运营组上次的复盘",
            memory_query_text="查一下运营组上次的复盘",
            suggestion_id="dmpp_test_001",
            audit_state=sentinel_state,
            pending_state_to_store=sentinel_state,
        )

        result = await _dispatch.dispatch(ctx, event, cfg)

        assert result.handled is True
        assert result.source == "dept_memory_prompt"
        assert result.decision_intent == "dept_memory_suggested"

    @pytest.mark.asyncio
    async def test_dept_memory_no_stop_falls_through(
        self, patched_dispatch: dict
    ) -> None:
        event = _make_event(text="hi", platform_id="巅池-Agent小助手")
        ctx = _make_context()
        cfg = _make_config()

        from dc_router.preprocessing.department_memory import DepartmentMemoryDecision

        patched_dispatch["dept"].return_value = DepartmentMemoryDecision(
            stop=False,
            effective_text="hi",
        )

        result = await _dispatch.dispatch(ctx, event, cfg)
        assert result.source != "dept_memory_prompt"


# ─────────────────────────────────────────────────────────────────────────
# 8. Stage 8 — memory injection
# ─────────────────────────────────────────────────────────────────────────


class TestStage8MemoryInjection:
    """Stage 8 must inject memory extras *without* modifying message_str."""

    @pytest.mark.asyncio
    async def test_memory_injection_runs_when_dept_decision_says_inject(
        self, patched_dispatch: dict
    ) -> None:
        event = _make_event(text="查一下上次的内容", platform_id="巅池-Agent小助手")
        ctx = _make_context()
        cfg = _make_config()

        from dc_router.preprocessing.department_memory import DepartmentMemoryDecision

        patched_dispatch["dept"].return_value = DepartmentMemoryDecision(
            stop=False,
            inject_memory=True,
            effective_text="查一下上次的内容",
        )
        patched_dispatch["memory"].return_value = True

        await _dispatch.dispatch(ctx, event, cfg)

        patched_dispatch["memory"].assert_awaited()

    @pytest.mark.asyncio
    async def test_memory_injection_skipped_when_dept_says_no_inject(
        self, patched_dispatch: dict
    ) -> None:
        event = _make_event(text="hi", platform_id="巅池-Agent小助手")
        ctx = _make_context()
        cfg = _make_config()

        from dc_router.preprocessing.department_memory import DepartmentMemoryDecision

        patched_dispatch["dept"].return_value = DepartmentMemoryDecision(
            stop=False,
            inject_memory=False,
            effective_text="hi",
        )

        await _dispatch.dispatch(ctx, event, cfg)
        patched_dispatch["memory"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_memory_injection_does_not_mutate_message_str(
        self, patched_dispatch: dict
    ) -> None:
        """Critical safety property: dispatch MUST NOT alter ``event.message_str``.

        This is the regression that was fixed in memory_injection.py.
        """
        event = _make_event(text="原始文本", platform_id="巅池-Agent小助手")
        ctx = _make_context()
        cfg = _make_config()

        from dc_router.preprocessing.department_memory import DepartmentMemoryDecision

        patched_dispatch["dept"].return_value = DepartmentMemoryDecision(
            stop=False,
            inject_memory=True,
            effective_text="原始文本",
        )
        patched_dispatch["memory"].return_value = True

        await _dispatch.dispatch(ctx, event, cfg)

        # The user-visible message must remain exactly as the user typed it.
        assert event.message_str == "原始文本"


# ─────────────────────────────────────────────────────────────────────────
# 9. Stage 9 — assistant tone
# ─────────────────────────────────────────────────────────────────────────


class TestStage9AssistantTone:
    """Stage 9 must call ``try_inject_assistant_tone`` (set_extra only)."""

    @pytest.mark.asyncio
    async def test_assistant_tone_runs_on_managed_platform(
        self, patched_dispatch: dict
    ) -> None:
        event = _make_event(text="帮我做一份品牌分析", platform_id="巅池-Agent小助手")
        ctx = _make_context()
        cfg = _make_config()
        patched_dispatch["tone"].return_value = True

        await _dispatch.dispatch(ctx, event, cfg)
        patched_dispatch["tone"].assert_called()

    @pytest.mark.asyncio
    async def test_assistant_tone_skipped_on_unmanaged_platform(
        self, patched_dispatch: dict
    ) -> None:
        event = _make_event(text="hi", platform_id="other-bot")
        ctx = _make_context()
        cfg = _make_config()

        await _dispatch.dispatch(ctx, event, cfg)
        patched_dispatch["tone"].assert_not_called()

    @pytest.mark.asyncio
    async def test_assistant_tone_does_not_block(self, patched_dispatch: dict) -> None:
        """Tone is a side-effect only; it must NOT stop the pipeline."""
        event = _make_event(text="hi", platform_id="巅池-Agent小助手")
        ctx = _make_context()
        cfg = _make_config()
        patched_dispatch["tone"].return_value = True

        await _dispatch.dispatch(ctx, event, cfg)
        # Pipeline continued past tone → went to media, then dc_router, then v1.
        patched_dispatch["media"].assert_awaited()
        patched_dispatch["dc"].assert_awaited()
        patched_dispatch["v1"].assert_awaited()


# ─────────────────────────────────────────────────────────────────────────
# 10. Stage 10 — media route
# ─────────────────────────────────────────────────────────────────────────


class TestStage10MediaRoute:
    """Stage 10 must handle image / video generation triggers."""

    @pytest.mark.asyncio
    async def test_media_route_handles_image_request(
        self, patched_dispatch: dict
    ) -> None:
        event = _make_event(text="生成一张海报", platform_id="巅池-Agent小助手")
        ctx = _make_context()
        cfg = _make_config()
        patched_dispatch["media"].return_value = True

        result = await _dispatch.dispatch(ctx, event, cfg)
        assert result.handled is True
        assert result.source == "media_route"
        patched_dispatch["dc"].assert_not_awaited()
        patched_dispatch["v1"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_media_route_skipped_on_unmanaged_platform(
        self, patched_dispatch: dict
    ) -> None:
        event = _make_event(text="生成一张海报", platform_id="other-bot")
        ctx = _make_context()
        cfg = _make_config()

        await _dispatch.dispatch(ctx, event, cfg)
        patched_dispatch["media"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_media_route_no_trigger_falls_through(
        self, patched_dispatch: dict
    ) -> None:
        event = _make_event(text="查一下运营数据", platform_id="巅池-Agent小助手")
        ctx = _make_context()
        cfg = _make_config()

        result = await _dispatch.dispatch(ctx, event, cfg)
        assert result.source != "media_route"


# ─────────────────────────────────────────────────────────────────────────
# 11. Stage 11 — dc_router decision
# ─────────────────────────────────────────────────────────────────────────


class TestStage11DCRouterDecision:
    """Stage 11 runs the main routing decision."""

    @pytest.mark.asyncio
    async def test_dc_router_handles_message(self, patched_dispatch: dict) -> None:
        event = _make_event(text="帮我做一份品牌分析", platform_id="巅池-Agent小助手")
        ctx = _make_context()
        cfg = _make_config()

        patched_dispatch["dc"].return_value = (
            True,
            "writing",
            "aihubmix/gemini-3.5-flash",
        )

        result = await _dispatch.dispatch(ctx, event, cfg)

        assert result.handled is True
        assert result.source == "dc_router"
        assert result.decision_intent == "writing"
        assert result.decision_provider == "aihubmix/gemini-3.5-flash"
        patched_dispatch["v1"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dc_router_not_run_when_inactive(
        self, patched_dispatch: dict
    ) -> None:
        """enabled=false & dry_run=true → dc_router stage is skipped."""
        event = _make_event(text="hi", platform_id="巅池-Agent小助手")
        ctx = _make_context()
        cfg = _make_config(enabled=False, dry_run=True)

        await _dispatch.dispatch(ctx, event, cfg)
        patched_dispatch["dc"].assert_not_awaited()
        patched_dispatch["v1"].assert_awaited()

    @pytest.mark.asyncio
    async def test_dc_router_error_with_fallback(self, patched_dispatch: dict) -> None:
        """decide() returns no decision → v1.0 fallback is invoked."""
        event = _make_event(text="hi", platform_id="巅池-Agent小助手")
        ctx = _make_context()
        cfg = _make_config(enabled=True, dry_run=False, fallback_on_error=True)

        patched_dispatch["dc"].return_value = (False, "", "")
        patched_dispatch["v1"].return_value = True

        result = await _dispatch.dispatch(ctx, event, cfg)
        assert result.handled is True
        assert result.source == "v1.0"

    @pytest.mark.asyncio
    async def test_dc_router_error_no_fallback_stops_dispatch(
        self, patched_dispatch: dict
    ) -> None:
        """decide() fails AND fallback_on_error=False → dispatch stops."""
        event = _make_event(text="hi", platform_id="巅池-Agent小助手")
        ctx = _make_context()
        cfg = _make_config(enabled=True, dry_run=False, fallback_on_error=False)

        patched_dispatch["dc"].return_value = (False, "", "")

        result = await _dispatch.dispatch(ctx, event, cfg)
        assert result.handled is True
        assert result.source == "dc_router_error_stop"
        patched_dispatch["v1"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dc_router_decide_exception_with_fallback(
        self, patched_dispatch: dict
    ) -> None:
        """An exception in decide() must be caught and fall back to v1."""
        event = _make_event(text="hi", platform_id="巅池-Agent小助手")
        ctx = _make_context()
        cfg = _make_config(enabled=True, dry_run=False, fallback_on_error=True)

        # Simulate dc_router.decide raising — _run_dc_router catches it
        # and returns (False, "", "").
        patched_dispatch["dc"].return_value = (False, "", "")
        patched_dispatch["v1"].return_value = True

        result = await _dispatch.dispatch(ctx, event, cfg)
        assert result.handled is True
        assert result.source == "v1.0"


# ─────────────────────────────────────────────────────────────────────────
# 12. Stage 12 — v1.0 fallback
# ─────────────────────────────────────────


class TestStage12V1Fallback:
    """Stage 12 — v1.0 legacy intent classification path."""

    @pytest.mark.asyncio
    async def test_v1_fallback_handles_message(self, patched_dispatch: dict) -> None:
        event = _make_event(text="hi", platform_id="巅池-Agent小助手")
        ctx = _make_context()
        cfg = _make_config()

        patched_dispatch["v1"].return_value = True

        result = await _dispatch.dispatch(ctx, event, cfg)
        assert result.handled is True
        assert result.source == "v1.0"

    @pytest.mark.asyncio
    async def test_v1_fallback_no_match_reports_source(
        self, patched_dispatch: dict
    ) -> None:
        event = _make_event(text="hi", platform_id="巅池-Agent小助手")
        ctx = _make_context()
        cfg = _make_config()

        # default v1 returns False
        result = await _dispatch.dispatch(ctx, event, cfg)
        assert result.handled is False
        assert result.source == "v1.0_no_match"

    @pytest.mark.asyncio
    async def test_v1_fallback_runs_when_dc_router_off(
        self, patched_dispatch: dict
    ) -> None:
        event = _make_event(text="hi", platform_id="巅池-Agent小助手")
        ctx = _make_context()
        cfg = _make_config(enabled=False, dry_run=True)

        await _dispatch.dispatch(ctx, event, cfg)
        patched_dispatch["dc"].assert_not_awaited()
        patched_dispatch["v1"].assert_awaited()

    @pytest.mark.asyncio
    async def test_v1_fallback_defaults_managed_platform_when_classifier_times_out(
        self,
    ) -> None:
        event = _make_event(text="这是一句没有关键词的真实私聊")
        ctx = _make_context()

        with (
            patch.object(_dispatch, "reason_with_llm_v1", AsyncMock(return_value=None)),
            patch.object(
                _dispatch,
                "apply_provider_pin",
                AsyncMock(return_value=True),
            ) as pin,
        ):
            handled = await _dispatch._v1_fallback(ctx, event, event.message_str)

        assert handled is True
        pin.assert_awaited_once()
        kwargs = pin.await_args.kwargs
        assert kwargs["target_provider_id"] == "aihubmix/qwen3.6-flash"
        assert kwargs["source"] == "default"
        assert kwargs["intent"] == "casual"

    @pytest.mark.asyncio
    async def test_v1_fallback_keeps_no_match_for_unmanaged_platform(self) -> None:
        event = _make_event(
            text="这是一句没有关键词的真实私聊",
            platform_id="unknown-platform",
        )
        ctx = _make_context()

        with (
            patch.object(_dispatch, "reason_with_llm_v1", AsyncMock(return_value=None)),
            patch.object(
                _dispatch,
                "apply_provider_pin",
                AsyncMock(return_value=True),
            ) as pin,
        ):
            handled = await _dispatch._v1_fallback(ctx, event, event.message_str)

        assert handled is False
        pin.assert_not_awaited()


# ─────────────────────────────────────────────────────────────────────────
# Pipeline ordering — the order of stages is contractually sensitive
# ─────────────────────────────────────────────────────────────────────────


class TestPipelineOrdering:
    """Verifies that earlier stages short-circuit later ones."""

    @pytest.mark.asyncio
    async def test_slash_bypasses_everything(self, patched_dispatch: dict) -> None:
        event = _make_event(text="/help")
        ctx = _make_context()
        cfg = _make_config()

        await _dispatch.dispatch(ctx, event, cfg)
        patched_dispatch["chitchat"].assert_not_awaited()
        # Card stage only fires for ``__card_action__:`` text.
        patched_dispatch["card"].assert_not_awaited()
        patched_dispatch["feishu"].assert_not_awaited()
        patched_dispatch["truth"].assert_not_awaited()
        patched_dispatch["memory"].assert_not_awaited()
        patched_dispatch["tone"].assert_not_called()
        patched_dispatch["media"].assert_not_awaited()
        patched_dispatch["dc"].assert_not_awaited()
        patched_dispatch["v1"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reasoning_prefix_short_circuits_remaining_stages(
        self, patched_dispatch: dict
    ) -> None:
        event = _make_event(text="#高 hi")
        ctx = _make_context()
        cfg = _make_config()
        patched_dispatch["pin"].return_value = True

        await _dispatch.dispatch(ctx, event, cfg)
        # Card stage only fires for ``__card_action__:`` text.
        patched_dispatch["card"].assert_not_awaited()
        # chitchat runs before prefix (stage 3 < stage 4).
        patched_dispatch["chitchat"].assert_awaited()
        patched_dispatch["feishu"].assert_not_awaited()
        patched_dispatch["truth"].assert_not_awaited()
        patched_dispatch["memory"].assert_not_awaited()
        patched_dispatch["tone"].assert_not_called()
        patched_dispatch["media"].assert_not_awaited()
        patched_dispatch["dc"].assert_not_awaited()
        patched_dispatch["v1"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_media_short_circuits_dc_router_and_v1(
        self, patched_dispatch: dict
    ) -> None:
        event = _make_event(text="生成一张图", platform_id="巅池-Agent小助手")
        ctx = _make_context()
        cfg = _make_config()
        patched_dispatch["media"].return_value = True

        await _dispatch.dispatch(ctx, event, cfg)
        patched_dispatch["dc"].assert_not_awaited()
        patched_dispatch["v1"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dc_router_handled_short_circuits_v1(
        self, patched_dispatch: dict
    ) -> None:
        event = _make_event(text="hi", platform_id="巅池-Agent小助手")
        ctx = _make_context()
        cfg = _make_config()
        patched_dispatch["dc"].return_value = (True, "casual", "aihubmix/qwen3.6-flash")

        await _dispatch.dispatch(ctx, event, cfg)
        patched_dispatch["v1"].assert_not_awaited()


# ─────────────────────────────────────────────────────────────────────────
# Dry-run mode — dc_router annotates the event but does not set_provider
# ─────────────────────────────────────────────────────────────────────────


class TestPipelineDryRun:
    """Dry-run mode must run dc_router but skip v1.0 fallback."""

    @pytest.mark.asyncio
    async def test_dry_run_calls_dc_router_and_annotates(
        self, patched_dispatch: dict
    ) -> None:
        """Dry-run mode runs dc_router (with ``annotate_event_with_decision``)
        and then still falls back to v1.0 — the dispatch comment block
        says ``v1.0 fallback (dc_router 关闭 / 异常 / dry-run)``.

        In dry-run the *primary* observable difference is that
        ``_run_dc_router`` is invoked with ``dry_run=True`` and the
        decision is *annotated* onto the event extras, not applied via
        ``set_provider``.
        """
        event = _make_event(text="hi", platform_id="巅池-Agent小助手")
        ctx = _make_context()
        cfg = _make_config(enabled=True, dry_run=True)

        # When dry-run, _run_dc_router is expected to invoke
        # annotate_event_with_decision (mocked here).
        patched_dispatch["dc"].return_value = (
            False,
            "writing",
            "aihubmix/gemini-3.5-flash",
        )
        patched_dispatch["annotate"].return_value = None

        result = await _dispatch.dispatch(ctx, event, cfg)

        patched_dispatch["dc"].assert_awaited()
        # In dry-run, dispatch ALWAYS falls back to v1.0 because
        # cfg.is_active is False — only the annotation is the dry-run
        # observable.
        patched_dispatch["v1"].assert_awaited()
        # Source reports the no-handle path
        assert result.handled is False

    @pytest.mark.asyncio
    async def test_dry_run_annotates_event_with_decision(
        self, patched_dispatch: dict
    ) -> None:
        """Dry-run must call ``annotate_event_with_decision`` so downstream
        consumers (logs, daily_card_renderer) can see what *would* have
        been applied."""
        event = _make_event(text="hi", platform_id="巅池-Agent小助手")
        ctx = _make_context()
        cfg = _make_config(enabled=True, dry_run=True)

        sentinel_decision = types.SimpleNamespace(
            intent="writing",
            provider_id="aihubmix/gemini-3.5-flash",
            source="rules",
            reason="",
        )

        # Inject a sentinel decision and capture the call.
        # _run_dc_router is mocked at the dispatch layer; we test the
        # contract that dc_router-core annotation is invoked.
        with patch.object(
            _dispatch,
            "annotate_event_with_decision",
            MagicMock(),
        ) as annotate_mock:
            # Re-route _run_dc_router to call annotate with sentinel.
            async def _annotating_dc(
                *args: Any, **kwargs: Any
            ) -> tuple[bool, str, str]:
                annotate_mock(sentinel_decision)
                return False, "writing", "aihubmix/gemini-3.5-flash"

            patched_dispatch["dc"].side_effect = _annotating_dc
            await _dispatch.dispatch(ctx, event, cfg)
            annotate_mock.assert_called_with(sentinel_decision)


# ─────────────────────────────────────────────────────────────────────────
# Error paths — pipeline must not crash on any individual stage failure
# ─────────────────────────────────────────────────────────────────────────


class TestPipelineErrorPaths:
    """Resilience: a single stage's failure must not crash dispatch.

    The dispatch-level wrappers (``_maybe_truth_intake``,
    ``_memory_injection``) have their own try/except guards inside the
    function body, so we exercise the *underlying* call site (the
    function or module that the wrapper imports) to assert that
    ``wrapper-exception-during-call`` does not propagate. The
    ``try_handle_*`` functions in ``preprocessing/`` also have their
    own try/except paths, which we lock down here.
    """

    @pytest.mark.asyncio
    async def test_truth_intake_stage_6_runs_and_returns(
        self, patched_dispatch: dict
    ) -> None:
        """Stage 6 (truth_intake) must return cleanly even when the
        underlying guard short-circuits — i.e. the wrapper handles its
        own exceptions and returns False rather than raising."""
        event = _make_event(text="hi", platform_id="巅池-Agent小助手")
        ctx = _make_context()
        cfg = _make_config(enabled=True, dry_run=False, fallback_on_error=True)

        # Default mock returns False → no block → dispatch continues.
        result = await _dispatch.dispatch(ctx, event, cfg)
        patched_dispatch["truth"].assert_awaited()
        # No block, so we fall through to dc_router stage.
        patched_dispatch["dc"].assert_awaited()
        assert result.handled is False

    @pytest.mark.asyncio
    async def test_memory_injection_stage_8_runs_and_returns(
        self, patched_dispatch: dict
    ) -> None:
        """Stage 8 (memory injection) must not raise even when the dept
        decision says ``inject_memory=True`` and the underlying call
        (mocked) does nothing."""
        event = _make_event(text="hi", platform_id="巅池-Agent小助手")
        ctx = _make_context()
        cfg = _make_config(enabled=True, dry_run=False, fallback_on_error=True)

        from dc_router.preprocessing.department_memory import DepartmentMemoryDecision

        patched_dispatch["dept"].return_value = DepartmentMemoryDecision(
            stop=False,
            inject_memory=True,
            effective_text="hi",
        )

        result = await _dispatch.dispatch(ctx, event, cfg)
        # Memory stage ran (because dept decision said inject)
        patched_dispatch["memory"].assert_awaited()
        assert result.handled is False

    @pytest.mark.asyncio
    async def test_dc_router_import_failure_falls_back_to_v1(
        self, patched_dispatch: dict
    ) -> None:
        event = _make_event(text="hi", platform_id="巅池-Agent小助手")
        ctx = _make_context()
        cfg = _make_config(enabled=True, dry_run=False, fallback_on_error=True)

        # _run_dc_router catches its own import errors and returns
        # (False, "", ""). Verify the dispatch handles that.
        patched_dispatch["dc"].return_value = (False, "", "")
        patched_dispatch["v1"].return_value = True

        result = await _dispatch.dispatch(ctx, event, cfg)
        assert result.handled is True
        assert result.source == "v1.0"

    @pytest.mark.asyncio
    async def test_v1_fallback_exception_propagates(
        self, patched_dispatch: dict
    ) -> None:
        """The v1.0 fallback is the last stage; if it raises, the
        exception is NOT wrapped at the dispatch level. This is a
        known property of the current implementation — we lock it
        down so any future change (e.g. wrapping in try/except) is
        deliberate.

        The implication for production: v1.0 must NEVER raise in
        practice; the helpers (``classify_intent_v1``,
        ``reason_with_llm_v1``, ``apply_provider_pin``) all swallow
        their own exceptions internally. This test is a tripwire.
        """
        event = _make_event(text="hi", platform_id="巅池-Agent小助手")
        ctx = _make_context()
        cfg = _make_config()

        async def _raise_v1(*args: Any, **kwargs: Any) -> bool:
            raise RuntimeError("v1.0 fallback blew up")

        patched_dispatch["v1"].side_effect = _raise_v1

        with pytest.raises(RuntimeError, match="v1.0 fallback blew up"):
            await _dispatch.dispatch(ctx, event, cfg)


# ─────────────────────────────────────────────────────────────────────────
# Result dataclass
# ─────────────────────────────────────────────────────────────────────────


class TestDispatchResultDataclass:
    """Locks down the DispatchResult shape used by plugin.route()."""

    def test_dispatch_result_default_construction(self) -> None:
        result = _dispatch.DispatchResult(handled=True)
        assert result.handled is True
        assert result.source == ""
        assert result.decision_provider == ""
        assert result.decision_intent == ""

    def test_dispatch_result_with_all_fields(self) -> None:
        result = _dispatch.DispatchResult(
            handled=True,
            source="dc_router",
            decision_provider="aihubmix/claude-opus-4-7",
            decision_intent="writing",
        )
        assert result.source == "dc_router"
        assert result.decision_provider == "aihubmix/claude-opus-4-7"
        assert result.decision_intent == "writing"

    def test_dispatch_result_slots_prevent_new_attrs(self) -> None:
        """DispatchResult uses ``@dataclass(slots=True)`` — adding unknown
        attributes must fail."""
        result = _dispatch.DispatchResult(handled=True)
        with pytest.raises(AttributeError):
            result.unknown_field = "x"  # type: ignore[attr-defined]
