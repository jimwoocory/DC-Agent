"""Engineering-grade tests for reasoning prefix parsing.

The reasoning prefix is the only short-circuit that bypasses
``DCRouter.decide()`` — it lets employees force a specific provider via
``#高`` / ``#超深`` / ``#codex`` etc. The dispatch stage 4 invokes
``match_reasoning_prefix(text)`` and, on a hit, calls
``apply_provider_pin`` directly.

This file locks down:

- :class:`TestReasoningPrefixMatch` — every documented prefix → provider_id
- :class:`TestReasoningPrefixResilience` — leading whitespace, casing, plain text
- :class:`TestStripKnownPrefix` — the prompt-side helper used by cli_handlers
- :class:`TestReasoningPrefixProviderAllowlist` — the table only references
  production provider_ids, no orphan entries
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

# Load reasoning_prefix without pulling in the full plugin tree.
_DC_AGENT_ROOT = Path(__file__).resolve().parents[2]
if str(_DC_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_DC_AGENT_ROOT))
_PLUGINS_PARENT = _DC_AGENT_ROOT / "data" / "plugins"
if str(_PLUGINS_PARENT) not in sys.path:
    sys.path.insert(0, str(_PLUGINS_PARENT))

import types  # noqa: E402

_dc_router_pkg = types.ModuleType("dc_router")
_dc_router_pkg.__path__ = [str(_PLUGINS_PARENT / "dc_router")]  # type: ignore[attr-defined]
sys.modules.setdefault("dc_router", _dc_router_pkg)
_routing_pkg = types.ModuleType("dc_router.routing")
_routing_pkg.__path__ = [str(_PLUGINS_PARENT / "dc_router" / "routing")]  # type: ignore[attr-defined]
sys.modules.setdefault("dc_router.routing", _routing_pkg)

_spec = importlib.util.spec_from_file_location(
    "dc_router.routing.reasoning_prefix",
    _PLUGINS_PARENT / "dc_router" / "routing" / "reasoning_prefix.py",
)
assert _spec is not None and _spec.loader is not None
_module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _module
_spec.loader.exec_module(_module)

REASONING_PREFIX_PROVIDERS = _module.REASONING_PREFIX_PROVIDERS
extract_codex_tool_request = _module.extract_codex_tool_request
match_reasoning_prefix = _module.match_reasoning_prefix
strip_known_prefix = _module.strip_known_prefix


def test_codex_tool_marker_is_distinct_from_existing_model_pins() -> None:
    assert extract_codex_tool_request("#codex工具 复核这个方案") == "复核这个方案"
    assert extract_codex_tool_request("  #CODEX工具 深度检查") == "深度检查"
    assert extract_codex_tool_request("#codex工具") == ""
    assert extract_codex_tool_request("#codex高 模型对照") is None
    assert match_reasoning_prefix("#codex高 模型对照") == "codex/gpt-5.5-high"


# ─────────────────────────────────────────────────────────────────────────
# 1. match_reasoning_prefix — every documented mapping
# ─────────────────────────────────────────────────────────────────────────


class TestReasoningPrefixMatch:
    """Locks down the deterministic prefix → provider_id mapping."""

    @staticmethod
    def _assert_match(text: str, expected_provider_id: str) -> None:
        provider_id = match_reasoning_prefix(text)
        assert provider_id == expected_provider_id, (
            f"prefix match for {text!r} expected {expected_provider_id!r}, "
            f"got {provider_id!r}"
        )

    def test_medium_prefix_routes_to_gemini_flash(self) -> None:
        self._assert_match("#中 帮我看看", "aihubmix/gemini-3.5-flash")
        self._assert_match("#medium 帮我看看", "aihubmix/gemini-3.5-flash")
        self._assert_match("#fast 帮我看看", "aihubmix/gemini-3.5-flash")

    def test_high_prefix_routes_to_claude_sonnet_46(self) -> None:
        self._assert_match("#高 帮我做完整分析", "aihubmix/claude-sonnet-4-6")
        self._assert_match("#high 帮我做完整分析", "aihubmix/claude-sonnet-4-6")

    def test_xhigh_prefix_routes_to_claude_opus_47(self) -> None:
        self._assert_match(
            "#超深 帮我做战略分析",
            "aihubmix/claude-opus-4-7",
        )
        self._assert_match(
            "#超高 帮我做战略分析",
            "aihubmix/claude-opus-4-7",
        )
        self._assert_match(
            "#xhigh 帮我做战略分析",
            "aihubmix/claude-opus-4-7",
        )
        self._assert_match(
            "#深度 帮我做战略分析",
            "aihubmix/claude-opus-4-7",
        )

    def test_codex_prefix_routes_to_codex_models(self) -> None:
        # Longest-prefix match: ``#codex超深`` wins over ``#codex`` so the
        # employee-typed escalation marker is honored — not silently
        # shadowed by the bare ``#codex`` baseline.
        self._assert_match("#codex 帮我看看", "codex/gpt-5.5-medium")
        self._assert_match("#codex高 帮我看看", "codex/gpt-5.5-high")
        self._assert_match("#codex超深 帮我看看", "codex/gpt-5.5-xhigh")

    def test_match_handles_leading_whitespace(self) -> None:
        # lstrip is applied before matching — leading spaces are ignored.
        self._assert_match("   #高 hi", "aihubmix/claude-sonnet-4-6")
        self._assert_match("\t#深度 hi", "aihubmix/claude-opus-4-7")

    def test_match_is_case_insensitive(self) -> None:
        self._assert_match("#HIGH hi", "aihubmix/claude-sonnet-4-6")
        self._assert_match("#深度 hi", "aihubmix/claude-opus-4-7")
        # CJK characters don't have casing — but the lookup is lowercased
        # for ASCII prefixes, so the comparison is robust.

    def test_match_returns_none_for_empty_input(self) -> None:
        assert match_reasoning_prefix("") is None

    def test_match_returns_none_for_plain_text(self) -> None:
        assert match_reasoning_prefix("hello") is None
        assert match_reasoning_prefix("random text") is None

    def test_match_returns_none_for_unrelated_hash(self) -> None:
        # The lookup is "startswith" against the table; ``#深度x`` would
        # match ``#深度`` (the entry in the table) — so this case actually
        # does match, not return None. We assert the no-match behavior only
        # for prefixes that share no prefix with any table entry.
        assert match_reasoning_prefix("#xyz 帮我看看") is None
        assert match_reasoning_prefix("## 帮我看看") is None

    def test_match_returns_none_for_unrelated_full_word(self) -> None:
        assert match_reasoning_prefix("#未知前缀 帮我看看") is None
        assert match_reasoning_prefix("#HelloWorld") is None

    def test_match_returns_none_for_only_whitespace(self) -> None:
        assert match_reasoning_prefix("   ") is None
        assert match_reasoning_prefix("\n\t") is None

    def test_longest_prefix_wins_over_shorter_prefix(self) -> None:
        """Regression: ``#codex超深`` MUST resolve to ``xhigh`` tier
        and not the bare ``#codex`` baseline. This test exists because
        an earlier first-match implementation shadowed the longer
        escalation marker — that is a real production bug because the
        employee explicitly typed the upgrade prefix."""
        # ``#codex超深`` is a 6-char prefix; ``#codex`` is a 5-char
        # prefix. The lookup must pick the longer one.
        result_highest = match_reasoning_prefix("#codex超深 帮我看看")
        result_medium = match_reasoning_prefix("#codex 帮我看看")
        assert result_highest == "codex/gpt-5.5-xhigh"
        assert result_medium == "codex/gpt-5.5-medium"
        assert result_highest != result_medium

    def test_xhigh_prefix_uses_claude_series_not_codex(self) -> None:
        """Critical contract: ``#超深`` / ``#超高`` / ``#xhigh`` / ``#深度``
        always route to the **Claude** family, never Codex. Only the
        ``#codex...`` family uses codex models — that is the cross-
        model comparison / fallback channel, not the default upgrade.

        This boundary is enforced by an explicit test because the
        prefixes ``#超深`` and ``#codex超深`` are textually very close;
        a careless edit to the table could let the wrong one win.
        """
        for text in (
            "#超深 战略分析",
            "#超高 战略分析",
            "#xhigh 战略分析",
            "#深度 战略分析",
        ):
            pinned = match_reasoning_prefix(text)
            assert pinned is not None, f"prefix {text!r} did not match"
            assert pinned.startswith("aihubmix/claude"), (
                f"#超深 family must route to Claude series, got {pinned!r} for {text!r}"
            )
            assert "codex" not in pinned, (
                f"#超深 family must never resolve to a codex model, "
                f"got {pinned!r} for {text!r}"
            )


# ─────────────────────────────────────────────────────────────────────────
# 2. The provider table itself
# ─────────────────────────────────────────────────────────────────────────


class TestReasoningPrefixProviderTable:
    """Locks down the integrity of REASONING_PREFIX_PROVIDERS."""

    def test_table_is_non_empty(self) -> None:
        assert len(REASONING_PREFIX_PROVIDERS) >= 5

    def test_table_keys_are_unique(self) -> None:
        keys = list(REASONING_PREFIX_PROVIDERS.keys())
        assert len(keys) == len(set(keys)), (
            f"duplicate keys in REASONING_PREFIX_PROVIDERS: {keys}"
        )

    def test_table_values_intentionally_collapse_aliases(self) -> None:
        """Aliases (``#高`` and ``#high``) collapse to one provider; the
        contract is that there is *no* alias collision that produces
        different provider_ids. We assert the document alias families:
        each CJK/ASCII alias pair points to the same provider."""
        alias_pairs = [
            ("#中", "#medium"),
            ("#高", "#high"),
            ("#超深", "#xhigh"),
            ("#超高", "#xhigh"),
            ("#深度", "#xhigh"),
            ("#快", "#fast"),
        ]
        for cjk, ascii_alias in alias_pairs:
            if (
                cjk in REASONING_PREFIX_PROVIDERS
                and ascii_alias in REASONING_PREFIX_PROVIDERS
            ):
                assert (
                    REASONING_PREFIX_PROVIDERS[cjk]
                    == REASONING_PREFIX_PROVIDERS[ascii_alias]
                ), f"alias pair {cjk!r}/{ascii_alias!r} disagree on provider_id"

    def test_table_values_match_match_function(self) -> None:
        """For each table key, the match function must return its value
        when the input is unambiguous (no other table entry is a longer
        prefix). Longest-match semantics guarantee this even for table
        entries that are themselves prefixes of longer entries."""
        for prefix, provider_id in REASONING_PREFIX_PROVIDERS.items():
            text = f"{prefix} sample task"
            actual = match_reasoning_prefix(text)
            # If a LONGER prefix matches our key (i.e. this key is a
            # strict prefix of another table entry) the match function
            # will return the longer one — by design. We only assert
            # the value when this prefix is itself the longest match.
            longer_collides = any(
                other != prefix and other.startswith(prefix)
                for other in REASONING_PREFIX_PROVIDERS
            )
            if longer_collides:
                continue
            assert actual == provider_id, (
                f"match_reasoning_prefix({text!r}) returned {actual!r}, "
                f"expected {provider_id!r}"
            )

    def test_table_values_use_allowlisted_providers(self) -> None:
        allowed_prefixes = (
            "aihubmix/",
            "cli/",
            "codex/",
        )
        for prefix, provider_id in REASONING_PREFIX_PROVIDERS.items():
            assert any(provider_id.startswith(p) for p in allowed_prefixes), (
                f"prefix {prefix!r} → {provider_id!r} not in allowlist"
            )


# ─────────────────────────────────────────────────────────────────────────
# 1b. Defensive invariant guard (2026-06-11)
# ─────────────────────────────────────────────────────────────────────────


class TestReasoningPrefixInvariantGuard:
    """The defensive ``logger.error`` invariant added on 2026-06-11 after the
    user reported that ``#超深`` was being routed to a codex model. We
    can't easily mutate the module's frozen table at runtime, so we patch
    ``_REASONING_PREFIX_PROVIDERS`` via ``monkeypatch.setattr`` on a
    freshly imported copy of the module — this also verifies the guard
    fires regardless of how the table became wrong.
    """

    def test_invariant_guard_fires_when_xhigh_prefix_breaks_to_codex(
        self, monkeypatch
    ) -> None:
        """If the table is corrupted so ``#超深`` → codex, the runtime
        guard must log an error. The match function should still return
        the (wrong) provider_id, but the error log makes the regression
        impossible to miss in production logs.

        We patch the already-imported module (``_module``) so we don't
        trigger the full dc_router plugin import chain (which has its
        own issues — see ``tests/dc_router/conftest.py`` if needed).

        Note: ``astrbot.api.logger`` does not propagate to the root
        logger by default (it ships its own handler stack), so we
        substitute it with a ``MagicMock`` to assert the call directly.
        This is more reliable than ``caplog`` for this case.
        """
        from unittest.mock import MagicMock  # noqa: PLC0415

        rp_mod = _module
        mock_logger = MagicMock()
        monkeypatch.setattr(rp_mod, "logger", mock_logger)

        broken = dict(rp_mod._REASONING_PREFIX_PROVIDERS)
        broken["#超深"] = "codex/gpt-5.5-xhigh"
        broken["#超高"] = "codex/gpt-5.5-high"
        broken["#xhigh"] = "codex/gpt-5.5-xhigh"
        broken["#深度"] = "codex/gpt-5.5-high"

        new_pairs = tuple((p.lower(), pid) for p, pid in broken.items())

        monkeypatch.setattr(rp_mod, "_REASONING_PREFIX_PROVIDERS", broken)
        monkeypatch.setattr(rp_mod, "_PREFIX_KEYS_LOWER", new_pairs)

        pinned = rp_mod.match_reasoning_prefix("#超深 战略分析")

        # 1) match function 还是返回了 (错误的) provider — guard 不阻断, 只打日志
        assert pinned == "codex/gpt-5.5-xhigh"
        # 2) 但 invariant guard 必须触发 logger.error, 且日志里能 grep 到 prefix / provider
        assert mock_logger.error.called, (
            "expected the defensive invariant guard to call logger.error "
            "when #超深 routes to a codex provider, but it never called it — "
            "the runtime guard is silently passing through a known-bad mapping"
        )
        all_messages = " ".join(
            str(call_args) for call_args in mock_logger.error.call_args_list
        )
        assert "REASONING PREFIX INVARIANT VIOLATED" in all_messages
        assert "#超深" in all_messages
        assert "codex" in all_messages

    def test_invariant_guard_does_not_fire_for_correct_table(self, monkeypatch) -> None:
        """Sanity: in the production-correct table, the guard must NOT
        fire — only the historical test logs the violation."""
        from unittest.mock import MagicMock  # noqa: PLC0415

        mock_logger = MagicMock()
        monkeypatch.setattr(_module, "logger", mock_logger)

        for prefix in ("#超深", "#超高", "#xhigh", "#深度"):
            _module.match_reasoning_prefix(f"{prefix} 任务")
        assert not mock_logger.error.called, (
            "the production table maps #超深 family to Claude Opus, "
            "so no invariant violation should ever call logger.error — "
            f"got calls: {mock_logger.error.call_args_list}"
        )


# ─────────────────────────────────────────────────────────────────────────
# 3. strip_known_prefix — the prompt-side helper
# ─────────────────────────────────────────────────────────────────────────


class TestStripKnownPrefix:
    """Locks down the strip_known_prefix helper used by cli_handlers."""

    def test_strip_known_prefix_removes_xhigh(self) -> None:
        assert strip_known_prefix("#深度 做一份品牌分析") == "做一份品牌分析"

    def test_strip_known_prefix_removes_codex(self) -> None:
        assert strip_known_prefix("#codex 帮我看看") == "帮我看看"

    def test_strip_known_prefix_removes_high(self) -> None:
        assert strip_known_prefix("#高 帮我做完整分析") == "帮我做完整分析"

    def test_strip_known_prefix_returns_unchanged_when_no_prefix(self) -> None:
        assert strip_known_prefix("hello") == "hello"
        assert strip_known_prefix("随机文本") == "随机文本"

    def test_strip_known_prefix_handles_empty(self) -> None:
        # Per the implementation, if stripping yields an empty string,
        # the original text is returned.
        assert strip_known_prefix("") == ""

    def test_strip_known_prefix_handles_leading_whitespace(self) -> None:
        assert strip_known_prefix("   #高 帮我做完整分析") == "帮我做完整分析"

    def test_strip_known_prefix_keeps_partial_match_intact(self) -> None:
        """A non-prefix word starting with '#' should not be stripped."""
        # "#未知前缀" is not in the known list
        assert strip_known_prefix("#未知前缀 帮我看看") == "#未知前缀 帮我看看"

    def test_strip_known_prefix_strips_longest_match(self) -> None:
        """The strip helper uses longest-match so ``#codex超深`` is fully
        consumed (not just ``#codex``) — this mirrors the routing
        behavior so the prompt builder never leaves a trailing
        ``超深`` lying in the body of the message."""
        assert strip_known_prefix("#codex超深 帮我看看") == "帮我看看"

    def test_strip_known_prefix_case_insensitive(self) -> None:
        assert strip_known_prefix("#HIGH hi") == "hi"


# ─────────────────────────────────────────────────────────────────────────
# 4. Re-export — back-compat alias
# ─────────────────────────────────────────────────────────────────────────


class TestReasoningPrefixExports:
    """Locks down the public re-export contract."""

    def test_reasoning_prefix_providers_is_exported(self) -> None:
        assert isinstance(REASONING_PREFIX_PROVIDERS, dict)
        assert all(
            isinstance(k, str) and isinstance(v, str)
            for k, v in REASONING_PREFIX_PROVIDERS.items()
        )

    def test_module_all_lists_public_symbols(self) -> None:
        all_exports = getattr(_module, "__all__", ())
        assert "REASONING_PREFIX_PROVIDERS" in all_exports
        assert "match_reasoning_prefix" in all_exports
        assert "strip_known_prefix" in all_exports
