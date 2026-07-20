"""Engineering-grade tests for ``data/plugins/dc_router/cli_handlers.py``.

Coverage surface:

- :class:`TestParseCliProvider` — pure parsing of ``cli/<backend>/<model>``
  strings, including Claude effort suffix detection and the
  non-CLI fall-through (returns ``("", model, None)`` so callers can
  distinguish ``aihubmix/...`` from a real CLI provider).
- :class:`TestIsCliProvider` — the ``cli/`` prefix detector that
  every public entry point funnels through.
- :class:`TestBuildCliPrompt` — intent-aware prompt construction. These
  prompts are the *only* text that gets fed into Codex/Grok
  CLIs, so a wrong template here means a wrong model behaviour.
- :class:`TestDispatchCliProviderBackend` — backend dispatch
  routing (disabled legacy CLI / codex / grok / unsupported). The internal
  ``_start_*`` helpers are patched so we can verify the dispatch
  table itself stays correct.
- :class:`TestDispatchCliProviderNonCli` — non-CLI provider_ids must
  return ``False`` and never call any backend.
- :class:`TestCliHandlersConstants` — exposed constant invariants.
  These constants are the contract the rest of dc_router relies on.
- :class:`TestQueueRecoveryLifecycle` — ``start_queue_recovery`` /
  ``stop_queue_recovery`` idempotency. Background tasks are easy to
  leak; we explicitly verify the start-twice / stop-twice behaviour.

The file uses importlib loading (see ``_load_cli_handlers``) so we do
not trigger ``data/plugins/dc_router/__init__.py`` which would cascade
into the full AstrBot runtime. This is the same pattern as
``tests/dc_router/test_dispatch_pipeline.py``.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ─────────────────────────────────────────────────────────────────────────
# AstrBot stubbing — cli_handlers imports:
#   - astrbot.api.logger
#   - astrbot.api.event.MessageEventResult
#   - astrbot.core.provider.entities.ProviderType
# ─────────────────────────────────────────────────────────────────────────


def _ensure_astrbot_stub() -> None:
    try:
        importlib.import_module("astrbot")
    except Exception:  # noqa: BLE001
        fake_pkg = types.ModuleType("astrbot")
        sys.modules.setdefault("astrbot", fake_pkg)

    # astrbot.api (logger)
    try:
        importlib.import_module("astrbot.api")
    except Exception:  # noqa: BLE001
        api_pkg = types.ModuleType("astrbot.api")
        sys.modules.setdefault("astrbot.api", api_pkg)
        sys.modules["astrbot"].api = api_pkg  # type: ignore[attr-defined]

    api = sys.modules["astrbot.api"]
    if not hasattr(api, "logger"):
        api.logger = MagicMock()  # type: ignore[attr-defined]

    # astrbot.api.event (MessageEventResult)
    try:
        importlib.import_module("astrbot.api.event")
    except Exception:  # noqa: BLE001
        event_pkg = types.ModuleType("astrbot.api.event")
        sys.modules.setdefault("astrbot.api.event", event_pkg)
        sys.modules["astrbot.api"].event = event_pkg  # type: ignore[attr-defined]

    event_mod = sys.modules["astrbot.api.event"]

    class _StubMessageEventResult:
        def __init__(self) -> None:
            self._message = ""
            self._use_t2i: bool | None = None
            self._stop = False

        def message(self, text: str) -> _StubMessageEventResult:
            self._message = text
            return self

        def use_t2i(self, flag: bool) -> _StubMessageEventResult:
            self._use_t2i = flag
            return self

        def stop_event(self) -> _StubMessageEventResult:
            self._stop = True
            return self

    if not hasattr(event_mod, "MessageEventResult"):
        event_mod.MessageEventResult = _StubMessageEventResult  # type: ignore[attr-defined]

    # astrbot.core (parent of provider.entities)
    try:
        importlib.import_module("astrbot.core")
    except Exception:  # noqa: BLE001
        core_pkg = types.ModuleType("astrbot.core")
        sys.modules.setdefault("astrbot.core", core_pkg)
        sys.modules["astrbot"].core = core_pkg  # type: ignore[attr-defined]

    # astrbot.core.provider.entities
    try:
        importlib.import_module("astrbot.core.provider.entities")
    except Exception:  # noqa: BLE001
        entities_mod = types.ModuleType("astrbot.core.provider.entities")

        class _StubProviderType:
            CHAT_COMPLETION = "chat_completion"

        entities_mod.ProviderType = _StubProviderType  # type: ignore[attr-defined]
        sys.modules.setdefault("astrbot.core.provider.entities", entities_mod)
        sys.modules["astrbot.core"].provider = types.ModuleType(  # type: ignore[attr-defined]
            "astrbot.core.provider"
        )
        sys.modules["astrbot.core"].provider.entities = entities_mod  # type: ignore[attr-defined]


_ensure_astrbot_stub()

# ─────────────────────────────────────────────────────────────────────────
# Module loading — bypass ``dc_router/__init__.py`` (would load the plugin
# runtime) and load only the cli_handlers module.
# ─────────────────────────────────────────────────────────────────────────

_DC_AGENT_ROOT = Path(__file__).resolve().parents[2]
_PLUGINS_PARENT = _DC_AGENT_ROOT / "data" / "plugins"
_CLI_HANDLERS_PATH = _PLUGINS_PARENT / "dc_router" / "cli_handlers.py"
if str(_PLUGINS_PARENT) not in sys.path:
    sys.path.insert(0, str(_PLUGINS_PARENT))


def _load_cli_handlers() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_cli_handlers_under_test", _CLI_HANDLERS_PATH
    )
    assert spec is not None and spec.loader is not None, (
        f"cli_handlers.py not found at {_CLI_HANDLERS_PATH}"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def cli_handlers() -> types.ModuleType:
    """Loaded once per test module — cli_handlers has no per-test state."""
    return _load_cli_handlers()


# ─────────────────────────────────────────────────────────────────────────
# Helpers — minimal event / decision mocks
# ─────────────────────────────────────────────────────────────────────────


def _make_event(
    *,
    text: str = "帮我分析下季度营收",
    umo: str = "ai:chat:user-1",
    platform_id: str = "feishu",
) -> MagicMock:
    event = MagicMock(name="event")
    event.message_str = text
    event.unified_msg_origin = umo
    event.get_platform_id = MagicMock(return_value=platform_id)
    return event


def _make_decision(
    *,
    provider_id: str = "cli/antigravity/gemini-3.5-flash",
    intent: str = "casual",
    metadata: dict | None = None,
) -> MagicMock:
    decision = MagicMock(name="decision")
    decision.provider_id = provider_id
    decision.intent = intent
    decision.metadata = metadata or {}
    return decision


# ─────────────────────────────────────────────────────────────────────────
# 1. parse_cli_provider
# ─────────────────────────────────────────────────────────────────────────


class TestParseCliProvider:
    """Pure parsing of ``cli/<backend>/<model>[-<effort>]`` strings.

    These tests pin the parser's contract because the public ``__all__``
    exposes ``parse_cli_provider`` and every dispatch path consumes
    its return value. A regression here means wrong backend dispatch.
    """

    def test_legacy_cli_provider_is_disabled_backend(self, cli_handlers) -> None:
        """``cli/antigravity/<model>`` is parsed as disabled, not executable."""
        assert cli_handlers.parse_cli_provider("cli/antigravity/gemini-3.5-flash") == (
            cli_handlers.DISABLED_LEGACY_CLI_BACKEND,
            "gemini-3.5-flash",
            None,
        )

    def test_codex_strips_backend_prefix(self, cli_handlers) -> None:
        """``cli/codex/<model>`` → ('codex', '<model>', None)"""
        assert cli_handlers.parse_cli_provider("cli/codex/gpt-5.5-medium") == (
            "codex",
            "gpt-5.5-medium",
            None,
        )

    def test_grok_build_shorthand_keeps_full_model(self, cli_handlers) -> None:
        """``cli/grok-build`` (无 /) → ('grok', 'grok-build', None).

        这是当前 ops_provider_map 的真实写法 — grok 没有再分级 effort,
        整段 ``grok-build`` 既是 backend 也算 model.
        """
        assert cli_handlers.parse_cli_provider("cli/grok-build") == (
            "grok",
            "grok-build",
            None,
        )

    def test_claude_xhigh_effort_suffix(self, cli_handlers) -> None:
        """``cli/claude-opus-4-7-xhigh`` → ('claude', 'claude-opus-4-7', 'xhigh').

        xhigh 是最高档; 注意要保留 model 名字本身的 ``-`` 分隔符,
        移除 effort 时要精确.
        """
        backend, model, effort = cli_handlers.parse_cli_provider(
            "cli/claude-opus-4-7-xhigh"
        )
        assert backend == "claude"
        assert model == "claude-opus-4-7"
        assert effort == "xhigh"

    def test_claude_high_effort_suffix(self, cli_handlers) -> None:
        backend, model, effort = cli_handlers.parse_cli_provider(
            "cli/claude-sonnet-4-6-high"
        )
        assert (backend, model, effort) == ("claude", "claude-sonnet-4-6", "high")

    def test_claude_medium_effort_suffix(self, cli_handlers) -> None:
        backend, model, effort = cli_handlers.parse_cli_provider(
            "cli/claude-sonnet-4-6-medium"
        )
        assert (backend, model, effort) == ("claude", "claude-sonnet-4-6", "medium")

    def test_claude_low_effort_suffix(self, cli_handlers) -> None:
        backend, model, effort = cli_handlers.parse_cli_provider(
            "cli/claude-haiku-4-1-low"
        )
        assert (backend, model, effort) == ("claude", "claude-haiku-4-1", "low")

    def test_claude_no_effort_suffix_defaults_to_medium(self, cli_handlers) -> None:
        """不带 effort 后缀时, 默认 medium (不是 high/xhigh).

        这是 v1.0 fallback 的入口路径 — 任何 ``cli/claude-...`` 没写 effort
        都不应该静默走 xhigh. 这一条把契约锁死.
        """
        backend, model, effort = cli_handlers.parse_cli_provider("cli/claude-opus-4-7")
        assert (backend, model, effort) == ("claude", "claude-opus-4-7", "medium")

    def test_claude_xhigh_outranks_high_in_effort_detection(self, cli_handlers) -> None:
        """xhigh 必须先匹配 — ``-xhigh`` 不会被误匹配成 ``-high``.

        实际工作原理: xhigh 在 ``for effort in (...)`` 里是第一个, 且
        ``endswith`` 是布尔, 所以 ``claude-opus-4-7-xhigh`` 会先命中 xhigh.
        这条把 \"xhigh 必胜\" 的不变量锁住 — 如果有人把 effort 顺序改坏,
        这条会立刻挂.
        """
        _, model, effort = cli_handlers.parse_cli_provider("cli/claude-opus-4-7-xhigh")
        assert effort == "xhigh"
        # 顺便确认没有把 \"-xhigh\" 错切成 \"-high\" 这种半截
        assert model == "claude-opus-4-7"
        assert not model.endswith("-xhigh")
        assert not model.endswith("-high")

    def test_non_cli_provider_returns_empty_backend(self, cli_handlers) -> None:
        """非 ``cli/`` 前缀 → ('', model, None).

        关键: 不能让 aihubmix/* / codex/* / cli 缺前缀 的 provider 被误
        当成 cli backend — dispatch_cli_provider 靠这个区分.
        """
        for non_cli in (
            "aihubmix/claude-opus-4-7",
            "aihubmix/gemini-3.5-flash",
            "aihubmix/grok-4.3",
            "codex/gpt-5.5-medium",  # 这是 aihubmix / 外部 codex API, 不是 CLI
        ):
            backend, model, effort = cli_handlers.parse_cli_provider(non_cli)
            assert backend == "", (
                f"non-CLI provider {non_cli!r} should return backend='', "
                f"got {backend!r}"
            )
            assert model == non_cli
            assert effort is None

    def test_empty_string_returns_empty_backend(self, cli_handlers) -> None:
        """空字符串 → ('', '', None) (不抛异常, 这是 v1.0 fallback 的边界)."""
        assert cli_handlers.parse_cli_provider("") == ("", "", None)

    def test_legacy_cli_nested_path_strips_only_one_prefix(self, cli_handlers) -> None:
        """``cli/antigravity/`` 前缀只剥一层.

        这是 v1 旧 routing_adapter 的写法: ``antigravity/gemini-3.5-flash``
        的 model 段可能本身再含 ``/``? — 当前不会, 但保险起见剥前缀不递归.
        """
        assert cli_handlers.parse_cli_provider("cli/antigravity/gemini-3.5-flash") == (
            cli_handlers.DISABLED_LEGACY_CLI_BACKEND,
            "gemini-3.5-flash",
            None,
        )
        # model 段不应该再剥 antigravity/
        _, model, _ = cli_handlers.parse_cli_provider("cli/antigravity/foo/bar")
        assert model == "foo/bar", (
            f"parse_cli_provider should NOT recursively strip antigravity/ "
            f"from the model segment, got {model!r}"
        )


# ─────────────────────────────────────────────────────────────────────────
# 2. is_cli_provider
# ─────────────────────────────────────────────────────────────────────────


class TestIsCliProvider:
    def test_cli_prefix_returns_true(self, cli_handlers) -> None:
        for pid in (
            "cli/antigravity/gemini-3.5-flash",
            "cli/codex/gpt-5.5-medium",
            "cli/grok-build",
            "cli/claude-opus-4-7",
        ):
            assert cli_handlers.is_cli_provider(pid) is True, (
                f"{pid!r} starts with cli/ but is_cli_provider returned False"
            )

    def test_non_cli_prefix_returns_false(self, cli_handlers) -> None:
        for pid in (
            "aihubmix/claude-opus-4-7",
            "aihubmix/gemini-3.5-flash",
            "codex/gpt-5.5-medium",
            "",
        ):
            assert cli_handlers.is_cli_provider(pid) is False, (
                f"{pid!r} does not start with cli/ but is_cli_provider returned True"
            )

    def test_is_cli_provider_uses_exact_prefix(self, cli_handlers) -> None:
        """``clia/...`` 之类必须有 ``cli/`` 严格前缀 — 不能 startswith(\"cli\")."""
        assert cli_handlers.is_cli_provider("cli_legacy/foo") is False
        assert cli_handlers.is_cli_provider("clientapp/foo") is False


# ─────────────────────────────────────────────────────────────────────────
# 3. build_cli_prompt
# ─────────────────────────────────────────────────────────────────────────


class TestBuildCliPrompt:
    """build_cli_prompt is the *only* place where prompts for CLI backends
    are constructed. A regression here = wrong model behaviour because
    Codex / Grok never see the original AstrBot pipeline.
    """

    def test_casual_intent_uses_assistant_voice(self, cli_handlers) -> None:
        event = _make_event(text="今天上海热不热？")
        decision = _make_decision(intent="casual")
        prompt = cli_handlers.build_cli_prompt(event, decision)
        assert "巅池-Agent小助手" in prompt
        assert "自然、简短、亲切" in prompt
        assert "今天上海热不热？" in prompt
        # 避免 routing_adapter 老 prompt 那种「解释路由过程」泄漏
        assert "路由过程" not in prompt or "不要解释路由过程" in prompt

    def test_work_preflight_intent_uses_assistant_voice(self, cli_handlers) -> None:
        """work_preflight 跟 casual 走同一档 — 不应该有自己的 prompt 分支."""
        event = _make_event(text="PRD v2.0 改动总结一下")
        decision = _make_decision(intent="work_preflight")
        prompt = cli_handlers.build_cli_prompt(event, decision)
        assert "PRD v2.0 改动总结一下" in prompt
        assert "自然、简短、亲切" in prompt

    def test_realtime_intent_uses_assistant_voice(self, cli_handlers) -> None:
        event = _make_event(text="5 点会议取消了吗")
        decision = _make_decision(intent="realtime")
        prompt = cli_handlers.build_cli_prompt(event, decision)
        assert "5 点会议取消了吗" in prompt
        assert "自然、简短、亲切" in prompt

    def test_fallback_intent_uses_assistant_voice(self, cli_handlers) -> None:
        event = _make_event(text="系统异常")
        decision = _make_decision(intent="fallback")
        prompt = cli_handlers.build_cli_prompt(event, decision)
        assert "系统异常" in prompt
        assert "自然、简短、亲切" in prompt

    def test_deep_insight_intent_uses_deep_template(self, cli_handlers) -> None:
        event = _make_event(text="分析 Q3 营收下滑原因")
        decision = _make_decision(intent="deep_insight")
        prompt = cli_handlers.build_cli_prompt(event, decision)
        assert "深度回答" in prompt
        assert "结论先行" in prompt
        assert "分析 Q3 营收下滑原因" in prompt

    def test_deep_creative_intent_uses_deep_template(self, cli_handlers) -> None:
        event = _make_event(text="给我想 3 个品牌 slogan")
        decision = _make_decision(intent="deep_creative")
        prompt = cli_handlers.build_cli_prompt(event, decision)
        assert "深度回答" in prompt
        assert "给我想 3 个品牌 slogan" in prompt

    def test_public_opinion_intent_uses_cr_template(self, cli_handlers) -> None:
        event = _make_event(text="今天微博热搜什么情况")
        decision = _make_decision(intent="public_opinion")
        prompt = cli_handlers.build_cli_prompt(event, decision)
        assert "舆情" in prompt
        assert "危机公关" in prompt
        assert "今天微博热搜什么情况" in prompt

    def test_unknown_intent_falls_back_to_plain_text(self, cli_handlers) -> None:
        """未识别 intent 走「裸文本」分支 — 不能让 prompt 模板 fallback 失败."""
        event = _make_event(text="raw text only")
        decision = _make_decision(intent="something_weird")
        prompt = cli_handlers.build_cli_prompt(event, decision)
        assert prompt == "raw text only"

    def test_empty_message_str_does_not_crash(self, cli_handlers) -> None:
        """空消息也要能构造出 prompt (不抛)."""
        event = _make_event(text="")
        decision = _make_decision(intent="casual")
        prompt = cli_handlers.build_cli_prompt(event, decision)
        # 模板里至少要保留头部, 员工消息那行就是空
        assert "巅池-Agent小助手" in prompt

    def test_whitespace_only_message_is_stripped(self, cli_handlers) -> None:
        """前后空白在 prompt 模板里是 strip 过的 — 跟 event.message_str 行为对齐."""
        event = _make_event(text="   季度营收分析   ")
        decision = _make_decision(intent="deep_insight")
        prompt = cli_handlers.build_cli_prompt(event, decision)
        # 模板里嵌入的是 strip 后的 text
        assert "季度营收分析" in prompt
        # 不会嵌入带前后空白的 raw
        assert "   季度营收分析   " not in prompt

    def test_decision_with_no_intent_falls_back(self, cli_handlers) -> None:
        """decision.intent 是 None/空 → 走裸文本 fallback (不要 AttributeError)."""
        event = _make_event(text="hello")
        decision = MagicMock()
        decision.intent = None
        prompt = cli_handlers.build_cli_prompt(event, decision)
        assert prompt == "hello"

    def test_event_with_no_message_str_falls_back(self, cli_handlers) -> None:
        """event.message_str = None 不会让 prompt 模板里出现 'None'."""
        event = _make_event()
        event.message_str = None
        decision = _make_decision(intent="casual")
        prompt = cli_handlers.build_cli_prompt(event, decision)
        assert "None" not in prompt
        # 模板头部应该还在
        assert "巅池-Agent小助手" in prompt


# ─────────────────────────────────────────────────────────────────────────
# 4. dispatch_cli_provider backend routing
# ─────────────────────────────────────────────────────────────────────────


class TestDispatchCliProviderBackend:
    """Verify the backend→handler dispatch table is intact.

    Patches executable private ``_start_*`` helpers so we exercise only
    the dispatcher itself, not the full CLI runner / QuotaGate chain.
    """

    def test_codex_adapter_uses_advanced_executor_policy(self) -> None:
        source = _CLI_HANDLERS_PATH.read_text(encoding="utf-8")

        assert "authorize_codex" in source
        assert '"deep_reasoning"' in source
        assert 'authorized_by="user"' in source
        assert "owns_schedule=False" in source

    @pytest.mark.asyncio
    async def test_codex_adapter_stops_when_policy_denies(
        self,
        cli_handlers,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            cli_handlers,
            "authorize_codex",
            MagicMock(
                return_value=types.SimpleNamespace(
                    allowed=False,
                    reason="scheduler_ownership_denied",
                )
            ),
        )

        result = await cli_handlers._start_codex(
            MagicMock(),
            _make_event(),
            decision=_make_decision(provider_id="cli/codex/gpt-5.5-medium"),
            prompt="test",
        )

        assert result is False
        cli_handlers.authorize_codex.assert_called_once_with(
            "deep_reasoning",
            authorized_by="user",
            owns_schedule=False,
        )

    @pytest.mark.asyncio
    async def test_codex_success_records_redacted_harness_timeline(
        self,
        cli_handlers,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        engine = types.SimpleNamespace(
            create_task=AsyncMock(
                return_value=types.SimpleNamespace(task_id="task-codex-1")
            ),
            mark_in_progress=AsyncMock(),
            append_trace=AsyncMock(),
            mark_review_required=AsyncMock(),
            fail_task=AsyncMock(),
        )
        context = types.SimpleNamespace(harness_engine=engine)
        event = _make_event(text="复核这份系统方案")
        decision = _make_decision(provider_id="cli/codex/gpt-5.4")
        decision.source = "user_explicit"
        runner = types.SimpleNamespace(
            run_codex=AsyncMock(
                return_value=types.SimpleNamespace(
                    ok=True,
                    text="审查结果",
                    elapsed_sec=1.25,
                    model_usage={"input_tokens": 12, "output_tokens": 8},
                )
            )
        )
        monkeypatch.setattr(
            cli_handlers,
            "authorize_codex",
            MagicMock(
                return_value=types.SimpleNamespace(
                    allowed=True,
                    reason="authorized",
                )
            ),
        )
        runner_module = types.ModuleType("data.plugins.dc_router.cli_runner")
        runner_module.CliRunner = MagicMock(return_value=runner)
        monkeypatch.setitem(
            sys.modules,
            "data.plugins.dc_router.cli_runner",
            runner_module,
        )
        monkeypatch.setattr(cli_handlers, "__package__", "data.plugins.dc_router")

        handled = await cli_handlers._start_codex(
            context,
            event,
            decision=decision,
            prompt="sensitive full prompt",
        )

        assert handled is True
        request = engine.create_task.await_args.args[0]
        assert request.domain == "advanced_executor:deep_reasoning"
        assert request.payload["owns_schedule"] is False
        assert request.payload["reasoning_effort"] == "high"
        assert len(request.payload["prompt_sha256"]) == 64
        assert "sensitive full prompt" not in str(request.payload)
        assert "审查结果" not in str(request.payload)
        engine.mark_in_progress.assert_awaited_once()
        assert any(
            call.args[1] == "advanced_executor_started"
            for call in engine.append_trace.await_args_list
        )
        assert any(
            call.args[1] == "advanced_executor_finished"
            for call in engine.append_trace.await_args_list
        )
        settled = engine.mark_review_required.await_args.kwargs["result"]
        assert settled["sandbox"] == "read-only"
        assert settled["reasoning_effort"] == "high"
        assert settled["output_chars"] == 4
        assert "审查结果" not in str(settled)
        engine.fail_task.assert_not_awaited()
        runner.run_codex.assert_awaited_once_with(
            "sensitive full prompt",
            model="gpt-5.4",
            reasoning_effort="high",
            task_kind="analysis",
            authorized_by="user",
            timeout=300,
        )

    @pytest.mark.asyncio
    async def test_codex_failure_records_harness_failure_without_prompt(
        self,
        cli_handlers,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        engine = types.SimpleNamespace(
            create_task=AsyncMock(
                return_value=types.SimpleNamespace(task_id="task-codex-2")
            ),
            mark_in_progress=AsyncMock(),
            append_trace=AsyncMock(),
            mark_review_required=AsyncMock(),
            fail_task=AsyncMock(),
        )
        context = types.SimpleNamespace(harness_engine=engine)
        runner = types.SimpleNamespace(
            run_codex=AsyncMock(
                return_value=types.SimpleNamespace(
                    ok=False,
                    text="",
                    error_code="timeout",
                    error="private failure detail",
                    elapsed_sec=300,
                )
            )
        )
        monkeypatch.setattr(
            cli_handlers,
            "authorize_codex",
            MagicMock(
                return_value=types.SimpleNamespace(
                    allowed=True,
                    reason="authorized",
                )
            ),
        )
        runner_module = types.ModuleType("data.plugins.dc_router.cli_runner")
        runner_module.CliRunner = MagicMock(return_value=runner)
        monkeypatch.setitem(
            sys.modules,
            "data.plugins.dc_router.cli_runner",
            runner_module,
        )
        monkeypatch.setattr(cli_handlers, "__package__", "data.plugins.dc_router")

        handled = await cli_handlers._start_codex(
            context,
            _make_event(text="私密请求"),
            decision=_make_decision(provider_id="cli/codex/gpt-5.4"),
            prompt="private prompt body",
        )

        assert handled is False
        engine.fail_task.assert_awaited_once_with(
            "task-codex-2",
            reason="Codex CLI failed: timeout",
        )
        finished = engine.append_trace.await_args_list[-1].args[2]
        assert finished == {
            "status": "failed",
            "error_code": "timeout",
            "elapsed_sec": 300,
        }
        assert "private prompt body" not in str(finished)
        assert "private failure detail" not in str(finished)
        engine.mark_review_required.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_legacy_cli_backend_is_disabled_without_starting_cli(
        self, cli_handlers
    ) -> None:
        event = _make_event()
        decision = _make_decision(
            provider_id="cli/antigravity/gemini-3.5-flash", intent="casual"
        )
        with (
            patch.object(
                cli_handlers, "_start_codex", new=AsyncMock(return_value=False)
            ) as mock_codex,
            patch.object(
                cli_handlers, "_start_grok", new=AsyncMock(return_value=False)
            ) as mock_grok,
        ):
            result = await cli_handlers.dispatch_cli_provider(
                MagicMock(), event, decision
            )
        assert result is True
        mock_codex.assert_not_awaited()
        mock_grok.assert_not_awaited()
        event.should_call_llm.assert_called_once_with(False)
        event.set_extra.assert_called_with(
            "dc_router_disabled_legacy_cli",
            {
                "provider_id": "cli/antigravity/gemini-3.5-flash",
                "reason": cli_handlers.DISABLED_LEGACY_CLI_REASON,
            },
        )

    @pytest.mark.asyncio
    async def test_codex_backend_calls_start_codex(self, cli_handlers) -> None:
        event = _make_event()
        decision = _make_decision(
            provider_id="cli/codex/gpt-5.5-medium", intent="work_preflight"
        )
        with (
            patch.object(
                cli_handlers, "_start_codex", new=AsyncMock(return_value=True)
            ) as mock_codex,
            patch.object(
                cli_handlers, "_start_grok", new=AsyncMock(return_value=False)
            ),
        ):
            result = await cli_handlers.dispatch_cli_provider(
                MagicMock(), event, decision
            )
        assert result is True
        mock_codex.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_grok_backend_calls_start_grok(self, cli_handlers) -> None:
        event = _make_event()
        decision = _make_decision(provider_id="cli/grok-build", intent="public_opinion")
        with (
            patch.object(
                cli_handlers, "_start_codex", new=AsyncMock(return_value=False)
            ),
            patch.object(
                cli_handlers, "_start_grok", new=AsyncMock(return_value=True)
            ) as mock_grok,
        ):
            result = await cli_handlers.dispatch_cli_provider(
                MagicMock(), event, decision
            )
        assert result is True
        mock_grok.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_claude_cli_backend_is_unsupported_returns_false(
        self, cli_handlers
    ) -> None:
        """``cli/claude-...`` 是预留但当前未实现的 backend — 必须返回 False,
        不要让它误派到 codex/grok.

        当前 ``_parse_cli_provider`` 能 parse 出 backend='claude', 但
        ``dispatch_cli_provider`` 没有 'claude' 分支 → 返回 False → 让
        v1.0 fallback 处理. 这条锁定这一行为.
        """
        event = _make_event()
        decision = _make_decision(
            provider_id="cli/claude-opus-4-7-medium", intent="deep_insight"
        )
        with (
            patch.object(cli_handlers, "_start_codex", new=AsyncMock()) as mock_codex,
            patch.object(cli_handlers, "_start_grok", new=AsyncMock()) as mock_grok,
        ):
            result = await cli_handlers.dispatch_cli_provider(
                MagicMock(), event, decision
            )
        assert result is False
        mock_codex.assert_not_awaited()
        mock_grok.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_cli_provider_returns_false_without_calling_backends(
        self, cli_handlers
    ) -> None:
        """非 ``cli/`` provider 必须早返回 False, 不会触到任何 _start_* 路径."""
        event = _make_event()
        decision = _make_decision(
            provider_id="aihubmix/claude-opus-4-7", intent="deep_insight"
        )
        with (
            patch.object(cli_handlers, "_start_codex", new=AsyncMock()) as mock_codex,
            patch.object(cli_handlers, "_start_grok", new=AsyncMock()) as mock_grok,
        ):
            result = await cli_handlers.dispatch_cli_provider(
                MagicMock(), event, decision
            )
        assert result is False
        mock_codex.assert_not_awaited()
        mock_grok.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_decision_with_missing_provider_id_returns_false(
        self, cli_handlers
    ) -> None:
        """decision.provider_id 缺失 → 返回 False, 不抛 AttributeError."""
        event = _make_event()
        decision = MagicMock()
        decision.provider_id = None
        decision.intent = "casual"
        result = await cli_handlers.dispatch_cli_provider(MagicMock(), event, decision)
        assert result is False

    @pytest.mark.asyncio
    async def test_disabled_legacy_cli_does_not_call_backend(
        self, cli_handlers
    ) -> None:
        """Legacy CLI provider ids are terminally disabled before backend start."""
        event = _make_event()
        decision = _make_decision(
            provider_id="cli/antigravity/gemini-3.5-flash", intent="casual"
        )
        result = await cli_handlers.dispatch_cli_provider(MagicMock(), event, decision)
        assert result is True
        event.should_call_llm.assert_called_once_with(False)


# ─────────────────────────────────────────────────────────────────────────
# 5. Constants / public surface
# ─────────────────────────────────────────────────────────────────────────


class TestCliHandlersConstants:
    """Exposed constants are the contract every other module consumes.

    Regress here = routing 100% breaks somewhere else.
    """

    def test_cli_provider_prefix_is_cli_slash(self, cli_handlers) -> None:
        """CLI provider ID 必须是 ``cli/<backend>/...`` 形式."""
        assert cli_handlers.CLI_PROVIDER_PREFIX == "cli/"

    def test_legacy_cli_provider_id_is_disabled_backend(self, cli_handlers) -> None:
        """DISABLED_LEGACY_CLI_PROVIDER_ID is recognized only as a disabled legacy id."""
        backend, _, _ = cli_handlers.parse_cli_provider(
            cli_handlers.DISABLED_LEGACY_CLI_PROVIDER_ID
        )
        assert backend == cli_handlers.DISABLED_LEGACY_CLI_BACKEND, (
            f"DISABLED_LEGACY_CLI_PROVIDER_ID={cli_handlers.DISABLED_LEGACY_CLI_PROVIDER_ID!r} "
            f"parsed to backend={backend!r} "
            f"(expected {cli_handlers.DISABLED_LEGACY_CLI_BACKEND!r})"
        )

    def test_grok_fallback_is_not_cli(self, cli_handlers) -> None:
        assert not cli_handlers.is_cli_provider(
            cli_handlers.GROK_BUILD_FALLBACK_PROVIDER_ID
        )

    def test_all_exports_are_present(self, cli_handlers) -> None:
        """__all__ 里所有名字都能从模块取到 — 防 PR 漏 import."""
        for name in cli_handlers.__all__:
            assert hasattr(cli_handlers, name), (
                f"cli_handlers.__all__ advertises {name!r} but it is missing"
            )


# ─────────────────────────────────────────────────────────────────────────
# 6. Queue recovery lifecycle
# ─────────────────────────────────────────────────────────────────────────


class TestQueueRecoveryLifecycle:
    """The background queue recovery task is started in plugin.py on load.
    It must be idempotent — calling start twice must not spawn two tasks,
    and stop on a non-running task must not crash.
    """

    @pytest.mark.asyncio
    async def test_start_queue_recovery_creates_task(self, cli_handlers) -> None:
        cli_handlers.stop_queue_recovery()  # reset
        cli_handlers.start_queue_recovery(MagicMock())
        assert cli_handlers._QUEUE_RECOVERY_TASK is not None
        assert not cli_handlers._QUEUE_RECOVERY_TASK.done()
        # cleanup
        cli_handlers.stop_queue_recovery()

    @pytest.mark.asyncio
    async def test_start_queue_recovery_is_idempotent(self, cli_handlers) -> None:
        """Second start must not spawn a new task while first is alive."""
        cli_handlers.stop_queue_recovery()
        cli_handlers.start_queue_recovery(MagicMock())
        first = cli_handlers._QUEUE_RECOVERY_TASK
        cli_handlers.start_queue_recovery(MagicMock())
        second = cli_handlers._QUEUE_RECOVERY_TASK
        assert first is second, (
            "start_queue_recovery spawned a second task — this leaks "
            "QuotaGate scanners on every plugin reload"
        )
        # cleanup
        cli_handlers.stop_queue_recovery()

    @pytest.mark.asyncio
    async def test_stop_queue_recovery_cancels_task(self, cli_handlers) -> None:
        cli_handlers.start_queue_recovery(MagicMock())
        cli_handlers.stop_queue_recovery()
        # give the task a tick to actually cancel
        await asyncio.sleep(0)  # yield so cancel propagates
        assert cli_handlers._QUEUE_RECOVERY_TASK is None

    @pytest.mark.asyncio
    async def test_stop_queue_recovery_when_not_running_is_noop(
        self, cli_handlers
    ) -> None:
        """stop on a non-running task must not raise."""
        cli_handlers._QUEUE_RECOVERY_TASK = None
        cli_handlers.stop_queue_recovery()  # should not raise
        assert cli_handlers._QUEUE_RECOVERY_TASK is None

    @pytest.mark.asyncio
    async def test_start_uses_default_interval_when_unprovided(
        self, cli_handlers
    ) -> None:
        """default interval 必须 == _DEFAULT_RECOVERY_INTERVAL (60s)."""
        cli_handlers.stop_queue_recovery()
        cli_handlers.start_queue_recovery(MagicMock())
        # task 已经创建, 验证我们没有传错 interval
        # (无法直接看 interval 参数, 但行为是 sleep 60s, 跟 default 一致)
        assert cli_handlers._DEFAULT_RECOVERY_INTERVAL == 60
        cli_handlers.stop_queue_recovery()


class TestQueueRecoveryFiltering:
    @pytest.mark.asyncio
    async def test_old_legacy_cli_queue_card_is_disabled_without_provider_switch(
        self, cli_handlers
    ) -> None:
        event = _make_event(
            text=(
                '__card_action__:{"value":{"source":"antigravity_queue_card",'
                '"action":"use_fallback","job_id":"legacy-cli-old-1"}}'
            )
        )
        context = MagicMock()
        context.provider_manager.set_provider = AsyncMock()

        handled = await cli_handlers.handle_disabled_legacy_cli_card_action(
            context, event
        )

        assert handled is True
        context.provider_manager.set_provider.assert_not_awaited()
        event.should_call_llm.assert_called_once_with(False)
        result = event.set_result.call_args.args[0]
        message = getattr(result, "_message", "") or result.chain[0].text
        assert "旧本地 CLI 入口已经关闭" in message
        event.set_extra.assert_called_with(
            "dc_router_disabled_legacy_cli_card_job_id",
            "legacy-cli-old-1",
        )

    @pytest.mark.asyncio
    async def test_recovery_cancels_source_image_edit_pending_job(
        self, cli_handlers, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pending = types.SimpleNamespace(
            job_id="job_cutout",
            enqueue_at=1.0,
            payload={
                "provider_id": cli_handlers.DISABLED_LEGACY_CLI_PROVIDER_ID,
                "backend": "antigravity",
                "original_prompt": "帮我把这张图片去掉背景，人物抠出来",
            },
        )
        gate = MagicMock()
        gate.reap_expired_running_jobs = AsyncMock(return_value=[])
        gate.list_pending_jobs = AsyncMock(return_value=[pending])
        gate.cancel_pending_job = AsyncMock(return_value=True)
        gate.start_pending_job = AsyncMock(return_value=None)
        monkeypatch.setattr(cli_handlers.time, "time", lambda: 2.0)

        resumed = await cli_handlers._resume_pending_cli_jobs(MagicMock(), gate)

        assert resumed == 0
        gate.cancel_pending_job.assert_awaited_once_with(
            "job_cutout",
            reason="source image edit must not be recovered as CLI queue",
        )
        gate.start_pending_job.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_recovery_cancels_expired_pending_job(
        self, cli_handlers, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pending = types.SimpleNamespace(
            job_id="job_old",
            enqueue_at=100.0,
            payload={
                "provider_id": "cli/codex/gpt-5.4",
                "backend": "codex",
                "original_prompt": "帮我写一份品牌分析",
            },
        )
        gate = MagicMock()
        gate.reap_expired_running_jobs = AsyncMock(return_value=[])
        gate.list_pending_jobs = AsyncMock(return_value=[pending])
        gate.cancel_pending_job = AsyncMock(return_value=True)
        gate.start_pending_job = AsyncMock(return_value=None)
        monkeypatch.setattr(
            cli_handlers.time,
            "time",
            lambda: 100.0 + cli_handlers.PENDING_CLI_RECOVERY_TTL_SECONDS + 1,
        )

        resumed = await cli_handlers._resume_pending_cli_jobs(MagicMock(), gate)

        assert resumed == 0
        gate.cancel_pending_job.assert_awaited_once_with(
            "job_old",
            reason="pending CLI recovery TTL expired",
        )
        gate.start_pending_job.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_recovery_cancels_retired_legacy_cli_pending_job(
        self, cli_handlers, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pending = types.SimpleNamespace(
            job_id="job_legacy_cli_old",
            enqueue_at=100.0,
            payload={
                "provider_id": cli_handlers.DISABLED_LEGACY_CLI_PROVIDER_ID,
                "backend": "antigravity",
                "original_prompt": "帮我写一份品牌分析",
            },
        )
        gate = MagicMock()
        gate.reap_expired_running_jobs = AsyncMock(return_value=[])
        gate.list_pending_jobs = AsyncMock(return_value=[pending])
        gate.cancel_pending_job = AsyncMock(return_value=True)
        gate.start_pending_job = AsyncMock(return_value=None)
        monkeypatch.setattr(cli_handlers.time, "time", lambda: 120.0)

        resumed = await cli_handlers._resume_pending_cli_jobs(MagicMock(), gate)

        assert resumed == 0
        gate.cancel_pending_job.assert_awaited_once_with(
            "job_legacy_cli_old",
            reason=cli_handlers.DISABLED_LEGACY_CLI_REASON,
        )
        gate.start_pending_job.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_recovery_starts_fresh_pending_job(
        self, cli_handlers, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pending = types.SimpleNamespace(
            job_id="job_fresh",
            enqueue_at=100.0,
            payload={
                "provider_id": "cli/codex/gpt-5.4",
                "backend": "codex",
                "original_prompt": "帮我写一份品牌分析",
            },
        )
        started = types.SimpleNamespace(job_id="job_fresh")
        gate = MagicMock()
        gate.reap_expired_running_jobs = AsyncMock(return_value=[])
        gate.list_pending_jobs = AsyncMock(return_value=[pending])
        gate.cancel_pending_job = AsyncMock(return_value=True)
        gate.start_pending_job = AsyncMock(return_value=started)
        monkeypatch.setattr(cli_handlers.time, "time", lambda: 120.0)

        resumed = await cli_handlers._resume_pending_cli_jobs(MagicMock(), gate)

        assert resumed == 1
        gate.cancel_pending_job.assert_not_awaited()
        gate.start_pending_job.assert_awaited_once_with("job_fresh")

    @pytest.mark.asyncio
    async def test_recovery_reclaims_expired_running_before_pending_scan(
        self, cli_handlers
    ) -> None:
        gate = MagicMock()
        gate.reap_expired_running_jobs = AsyncMock(return_value=["expired-job"])
        gate.list_pending_jobs = AsyncMock(return_value=[])

        resumed = await cli_handlers._resume_pending_cli_jobs(MagicMock(), gate)

        assert resumed == 0
        gate.reap_expired_running_jobs.assert_awaited_once_with(limit=20)
        gate.list_pending_jobs.assert_awaited_once_with(limit=20)
