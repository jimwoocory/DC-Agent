"""Engineering-grade tests for ``dc_router.config``.

``load_config`` is the single source of truth for routing runtime
configuration — every hot-reload read goes through it. The function
must NEVER raise on a malformed file because the plugin loop runs on
every message; an exception would cascade into message-processing
failures. We exercise every failure mode explicitly:

- :class:`TestLoadConfigFileNotFound` — missing file → safe defaults
- :class:`TestLoadConfigCorruptedJson` — bad JSON → safe defaults
- :class:`TestLoadConfigValidFile` — well-formed JSON → parses
- :class:`TestLoadConfigLegacyFields` — old v1.0 keys are dropped
- :class:`TestLoadConfigEdgeCaseTypes` — wrong-typed fields normalised
- :class:`TestDCRouterConfigProperties` — ``is_active`` / ``is_dry_run``
- :class:`TestFeishuChannelRoute` — agent → provider resolution
- :class:`TestPlatformHelpers` — business / ops / managed platform sets
"""

from __future__ import annotations

import importlib
import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Reuse the same module-stubbing strategy as the dispatch test so we
# can import ``dc_router.config`` in isolation.
_DC_AGENT_ROOT = Path(__file__).resolve().parents[2]
_PLUGINS_PARENT = _DC_AGENT_ROOT / "data" / "plugins"
if str(_DC_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_DC_AGENT_ROOT))
if str(_PLUGINS_PARENT) not in sys.path:
    sys.path.insert(0, str(_PLUGINS_PARENT))


# Stub astrbot so ``dc_router.config`` (which imports ``astrbot.api``)
# loads cleanly.
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
sys.modules["astrbot.api"].logger = MagicMock()  # type: ignore[attr-defined]

# Stub the parent ``dc_router`` package (the real ``__init__`` would
# import the Star plugin which depends on the live runtime).
_dc_router_pkg_stub = types.ModuleType("dc_router")
_dc_router_pkg_stub.__path__ = [str(_PLUGINS_PARENT / "dc_router")]  # type: ignore[attr-defined]
_dc_router_pkg_stub.__file__ = str(  # type: ignore[attr-defined]
    _PLUGINS_PARENT / "dc_router" / "__init__.py"
)
sys.modules["dc_router"] = _dc_router_pkg_stub

_config_module = importlib.import_module("dc_router.config")
DCRouterConfig = _config_module.DCRouterConfig
load_config = _config_module.load_config
BUSINESS_PLATFORM_IDS = _config_module.BUSINESS_PLATFORM_IDS
OPS_PLATFORM_IDS = _config_module.OPS_PLATFORM_IDS
is_business_platform = _config_module.is_business_platform
is_ops_platform = _config_module.is_ops_platform
is_dc_router_managed_platform = _config_module.is_dc_router_managed_platform


# ─────────────────────────────────────────────────────────────────────────
# 1. File not found — safe defaults
# ─────────────────────────────────────────────────────────────────────────


class TestLoadConfigFileNotFound:
    """``load_config`` must return a default DCRouterConfig when the
    file does not exist. This is the most common cold-start path."""

    def test_missing_file_returns_safe_defaults(self, tmp_path: Path) -> None:
        missing = tmp_path / "does_not_exist.json"
        cfg = load_config(missing)
        assert isinstance(cfg, DCRouterConfig)
        # Default: enabled=False, dry_run=True, fallback_on_error=True
        assert cfg.enabled is False
        assert cfg.dry_run is True
        assert cfg.fallback_on_error is True
        # The path should be reflected back on the config object
        assert cfg.config_path == missing
        # No feishu routes were loaded
        assert cfg.feishu_channel_routes == {}
        # The default recovery interval is 60 seconds
        assert cfg.queue_recovery_interval_seconds == 60

    def test_missing_file_uses_provided_path_as_config_path(
        self, tmp_path: Path
    ) -> None:
        sentinel = tmp_path / "dc_router_config.json"
        cfg = load_config(sentinel)
        assert cfg.config_path == sentinel

    def test_none_path_falls_back_to_module_default(self) -> None:
        """``load_config(None)`` falls back to ``CONFIG_PATH``."""
        cfg = load_config(None)
        # The module default is the production path; we don't assert
        # on its exact value, only that no exception is raised.
        assert isinstance(cfg, DCRouterConfig)


# ─────────────────────────────────────────────────────────────────────────
# 2. Corrupted JSON — safe defaults
# ─────────────────────────────────────────────────────────────────────────


class TestLoadConfigCorruptedJson:
    """``load_config`` must NEVER raise on a malformed file."""

    def test_truncated_json_returns_safe_defaults(self, tmp_path: Path) -> None:
        broken = tmp_path / "broken.json"
        broken.write_text('{"enabled": true, "dry_run":', encoding="utf-8")
        cfg = load_config(broken)
        assert cfg.enabled is False
        assert cfg.dry_run is True
        assert cfg.fallback_on_error is True

    def test_garbage_content_returns_safe_defaults(self, tmp_path: Path) -> None:
        broken = tmp_path / "garbage.json"
        broken.write_text("not json at all", encoding="utf-8")
        cfg = load_config(broken)
        assert cfg.enabled is False
        assert cfg.dry_run is True

    def test_empty_file_returns_safe_defaults(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty.json"
        empty.write_text("", encoding="utf-8")
        cfg = load_config(empty)
        assert cfg.enabled is False
        assert cfg.dry_run is True

    def test_json_array_returns_safe_defaults(self, tmp_path: Path) -> None:
        """A JSON array (not an object) is not a valid config — it
        parses but yields no keys, so every field falls back to its
        default."""
        array_file = tmp_path / "array.json"
        array_file.write_text("[1, 2, 3]", encoding="utf-8")
        cfg = load_config(array_file)
        # The object access on data.get(...) doesn't raise on a list
        # because ``.get`` is only defined on dicts. Actually it WILL
        # raise AttributeError — we test that the loader catches that
        # and returns defaults.
        assert cfg.enabled is False

    def test_os_error_on_read_returns_safe_defaults(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the file disappears between ``exists()`` and ``open()`` —
        a TOCTOU race — the loader must return defaults rather than
        raise. We simulate this by making ``open`` raise ``OSError``."""
        present = tmp_path / "racy.json"
        present.write_text("{}", encoding="utf-8")

        real_open = open

        def _flaky_open(*args: object, **kwargs: object) -> object:
            if str(args[0]) == str(present):
                raise OSError("simulated race")
            return real_open(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr("builtins.open", _flaky_open)
        cfg = load_config(present)
        assert isinstance(cfg, DCRouterConfig)
        assert cfg.enabled is False


# ─────────────────────────────────────────────────────────────────────────
# 3. Valid file — fields are parsed correctly
# ─────────────────────────────────────────────────────────────────────────


class TestLoadConfigValidFile:
    """Happy-path: every documented field is read end-to-end."""

    def test_full_config_parses_all_fields(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "full.json"
        cfg_file.write_text(
            json.dumps(
                {
                    "enabled": True,
                    "dry_run": False,
                    "fallback_on_error": False,
                    "feishu_channel_routes": {
                        "planning-agent": "aihubmix/gemini-3.5-flash",
                        "code-agent": {
                            "provider_id": "codex/gpt-5.5-high",
                            "reasoning_tier": "high",
                        },
                    },
                    "queue_recovery_interval_seconds": 30,
                }
            ),
            encoding="utf-8",
        )
        cfg = load_config(cfg_file)
        assert cfg.enabled is True
        assert cfg.dry_run is False
        assert cfg.fallback_on_error is False
        assert cfg.queue_recovery_interval_seconds == 30
        assert "planning-agent" in cfg.feishu_channel_routes
        assert "code-agent" in cfg.feishu_channel_routes

    def test_utf8_bom_config_parses(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "bom.json"
        cfg_file.write_text(
            json.dumps({"enabled": True, "dry_run": True}),
            encoding="utf-8-sig",
        )
        cfg = load_config(cfg_file)
        assert cfg.enabled is True
        assert cfg.dry_run is True

    def test_enabled_field_accepts_truthy_value(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "enabled.json"
        cfg_file.write_text(json.dumps({"enabled": True}), encoding="utf-8")
        cfg = load_config(cfg_file)
        assert cfg.enabled is True

    def test_dry_run_field_is_parsed(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "dry_run.json"
        cfg_file.write_text(json.dumps({"dry_run": True}), encoding="utf-8")
        cfg = load_config(cfg_file)
        assert cfg.dry_run is True

    def test_fallback_on_error_field_is_parsed(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "fallback.json"
        cfg_file.write_text(json.dumps({"fallback_on_error": False}), encoding="utf-8")
        cfg = load_config(cfg_file)
        assert cfg.fallback_on_error is False

    def test_last_loaded_at_is_set_after_load(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "mtime.json"
        cfg_file.write_text("{}", encoding="utf-8")
        cfg = load_config(cfg_file)
        assert cfg.last_loaded_at > 0


# ─────────────────────────────────────────────────────────────────────────
# 4. Legacy fields (v1.0) — silently dropped
# ─────────────────────────────────────────────────────────────────────────


class TestLoadConfigLegacyFields:
    """Old v1.0 keys (PREFIX_INTENTS / INTENT_TO_PROVIDER / REASONING_PREFIX_PROVIDERS)
    must be ignored; only the dc_router_core routing table is honoured."""

    def test_prefix_intents_legacy_field_ignored(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "legacy.json"
        cfg_file.write_text(
            json.dumps(
                {
                    "PREFIX_INTENTS": {"#深度": "deep"},
                    "INTENT_TO_PROVIDER": {"deep": "aihubmix/claude-opus-4-7"},
                    "enabled": True,
                }
            ),
            encoding="utf-8",
        )
        cfg = load_config(cfg_file)
        # enabled is parsed; legacy fields are dropped.
        assert cfg.enabled is True
        # No way to access the legacy dict from the dataclass — the
        # load function logs a notice but does not store it.

    def test_reasoning_prefix_providers_legacy_field_ignored(
        self, tmp_path: Path
    ) -> None:
        cfg_file = tmp_path / "legacy2.json"
        cfg_file.write_text(
            json.dumps(
                {
                    "REASONING_PREFIX_PROVIDERS": {"#深度": "aihubmix/claude-opus-4-7"},
                }
            ),
            encoding="utf-8",
        )
        cfg = load_config(cfg_file)
        # Defaults are intact — no exception, no key promotion.
        assert cfg.enabled is False


# ─────────────────────────────────────────────────────────────────────────
# 5. Edge-case types — coerced to safe defaults
# ─────────────────────────────────────────────────────────────────────────


class TestLoadConfigEdgeCaseTypes:
    """Wrong-typed fields must be normalised to safe defaults, not crash."""

    def test_feishu_channel_routes_as_list_is_dropped(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "bad_routes.json"
        cfg_file.write_text(
            json.dumps({"feishu_channel_routes": ["not", "a", "dict"]}),
            encoding="utf-8",
        )
        cfg = load_config(cfg_file)
        assert cfg.feishu_channel_routes == {}

    def test_queue_recovery_negative_integer_replaced_with_default(
        self, tmp_path: Path
    ) -> None:
        cfg_file = tmp_path / "bad_interval.json"
        cfg_file.write_text(
            json.dumps({"queue_recovery_interval_seconds": -10}),
            encoding="utf-8",
        )
        cfg = load_config(cfg_file)
        # The default of 60 is applied when the value is non-int or
        # negative.
        assert cfg.queue_recovery_interval_seconds == 60

    def test_queue_recovery_string_replaced_with_default(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "str_interval.json"
        cfg_file.write_text(
            json.dumps({"queue_recovery_interval_seconds": "30"}),
            encoding="utf-8",
        )
        cfg = load_config(cfg_file)
        # String-typed interval is not a valid int — default wins.
        assert cfg.queue_recovery_interval_seconds == 60

    def test_enabled_string_truthy_value_is_coerced(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "str_enabled.json"
        cfg_file.write_text(json.dumps({"enabled": "true"}), encoding="utf-8")
        cfg = load_config(cfg_file)
        # ``bool("true")`` is True — Python's truthiness picks it up.
        assert cfg.enabled is True


# ─────────────────────────────────────────────────────────────────────────
# 6. DCRouterConfig dataclass properties
# ─────────────────────────────────────────────────────────────────────────


class TestDCRouterConfigProperties:
    """``is_active`` and ``is_dry_run`` reflect the routing gate."""

    def test_is_active_true_when_enabled_and_not_dry_run(self) -> None:
        cfg = DCRouterConfig(enabled=True, dry_run=False)
        assert cfg.is_active is True
        assert cfg.is_dry_run is False

    def test_is_active_false_when_dry_run_even_if_enabled(self) -> None:
        cfg = DCRouterConfig(enabled=True, dry_run=True)
        assert cfg.is_active is False
        assert cfg.is_dry_run is True

    def test_is_active_false_when_disabled(self) -> None:
        cfg = DCRouterConfig(enabled=False, dry_run=False)
        assert cfg.is_active is False
        assert cfg.is_dry_run is False

    def test_default_construction_is_safe(self) -> None:
        """A bare DCRouterConfig() must yield the safe defaults
        (disabled, dry-run on, fallback on). This is the behaviour
        ``load_config`` falls back to on a missing/corrupt file."""
        cfg = DCRouterConfig()
        assert cfg.enabled is False
        assert cfg.dry_run is True
        assert cfg.fallback_on_error is True
        assert cfg.is_active is False

    def test_slots_prevent_unknown_attributes(self) -> None:
        """``DCRouterConfig`` is ``@dataclass(slots=True)`` — adding
        unknown attributes must fail (the loader does not allow
        extension of the surface)."""
        cfg = DCRouterConfig()
        with pytest.raises(AttributeError):
            cfg.unknown_field = "x"  # type: ignore[attr-defined]


# ─────────────────────────────────────────────────────────────────────────
# 7. feishu_channel_routes resolution
# ─────────────────────────────────────────────────────────────────────────


class TestFeishuChannelRoute:
    """``route_for_feishu_channel`` accepts both string and dict shapes."""

    def test_string_route_value_resolves(self) -> None:
        cfg = DCRouterConfig(
            feishu_channel_routes={"planning-agent": "aihubmix/gemini-3.5-flash"}
        )
        route = cfg.route_for_feishu_channel("planning-agent")
        assert route == {
            "provider_id": "aihubmix/gemini-3.5-flash",
            "reasoning_tier": "",
        }

    def test_dict_route_value_with_provider_id_resolves(self) -> None:
        cfg = DCRouterConfig(
            feishu_channel_routes={
                "code-agent": {
                    "provider_id": "codex/gpt-5.5-high",
                    "reasoning_tier": "high",
                }
            }
        )
        route = cfg.route_for_feishu_channel("code-agent")
        assert route == {
            "provider_id": "codex/gpt-5.5-high",
            "reasoning_tier": "high",
        }

    def test_dict_route_value_without_provider_id_returns_none(self) -> None:
        cfg = DCRouterConfig(
            feishu_channel_routes={"empty-agent": {"reasoning_tier": "high"}}
        )
        assert cfg.route_for_feishu_channel("empty-agent") is None

    def test_unknown_agent_returns_none(self) -> None:
        cfg = DCRouterConfig(feishu_channel_routes={})
        assert cfg.route_for_feishu_channel("missing") is None

    def test_empty_string_provider_id_returns_none(self) -> None:
        cfg = DCRouterConfig(feishu_channel_routes={"agent": "   "})
        assert cfg.route_for_feishu_channel("agent") is None

    def test_unsupported_route_value_type_returns_none(self) -> None:
        cfg = DCRouterConfig(feishu_channel_routes={"agent": 12345})
        # An integer is neither a string nor a dict → returns None.
        assert cfg.route_for_feishu_channel("agent") is None


# ─────────────────────────────────────────────────────────────────────────
# 8. Platform ID helpers
# ─────────────────────────────────────────────────────────────────────────


class TestPlatformHelpers:
    """Locks down the business / ops / managed platform id sets."""

    def test_business_platform_ids_contains_documented_bots(self) -> None:
        assert "巅池-Agent小助手" in BUSINESS_PLATFORM_IDS

    def test_ops_platform_ids_contains_documented_bots(self) -> None:
        assert "巅池-技术（DevOps）" in OPS_PLATFORM_IDS
        assert "巅池-技术" in OPS_PLATFORM_IDS

    def test_business_and_ops_sets_are_disjoint(self) -> None:
        """A platform cannot be both business and ops — that would
        confuse the router_mode decision."""
        assert BUSINESS_PLATFORM_IDS.isdisjoint(OPS_PLATFORM_IDS)

    def test_is_business_platform_true_for_business_id(self) -> None:
        assert is_business_platform("巅池-Agent小助手") is True

    def test_is_business_platform_false_for_ops_id(self) -> None:
        assert is_business_platform("巅池-技术（DevOps）") is False

    def test_is_business_platform_false_for_unknown(self) -> None:
        assert is_business_platform("random-bot") is False

    def test_is_ops_platform_true_for_ops_id(self) -> None:
        assert is_ops_platform("巅池-技术（DevOps）") is True

    def test_is_ops_platform_false_for_business_id(self) -> None:
        assert is_ops_platform("巅池-Agent小助手") is False

    def test_is_dc_router_managed_platform_true_for_either(self) -> None:
        assert is_dc_router_managed_platform("巅池-Agent小助手") is True
        assert is_dc_router_managed_platform("巅池-技术（DevOps）") is True
        assert is_dc_router_managed_platform("巅池-技术") is True

    def test_is_dc_router_managed_platform_false_for_unknown(self) -> None:
        assert is_dc_router_managed_platform("random-bot") is False

    def test_managed_platform_is_union_of_business_and_ops(self) -> None:
        """``is_dc_router_managed_platform`` must be the union of
        business and ops — no platform should be classified as
        managed that isn't in either set."""
        managed = BUSINESS_PLATFORM_IDS | OPS_PLATFORM_IDS
        for plat in managed:
            assert is_dc_router_managed_platform(plat) is True
        for plat in ("random-bot", "another", ""):
            assert is_dc_router_managed_platform(plat) is False


# ─────────────────────────────────────────────────────────────────────────
# 9. End-to-end: tmp_path → load_config → config object roundtrip
# ─────────────────────────────────────────────────────────────────────────


class TestLoadConfigRoundtrip:
    """A full roundtrip — write JSON, read it back, assert fields."""

    def test_roundtrip_preserves_documented_fields(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "roundtrip.json"
        original = {
            "enabled": True,
            "dry_run": False,
            "fallback_on_error": True,
            "feishu_channel_routes": {
                "planning-agent": "aihubmix/gemini-3.5-flash",
                "code-agent": {
                    "provider_id": "codex/gpt-5.5-high",
                    "reasoning_tier": "high",
                },
            },
            "queue_recovery_interval_seconds": 90,
        }
        cfg_file.write_text(json.dumps(original), encoding="utf-8")

        cfg = load_config(cfg_file)
        assert cfg.enabled == original["enabled"]
        assert cfg.dry_run == original["dry_run"]
        assert cfg.fallback_on_error == original["fallback_on_error"]
        assert cfg.queue_recovery_interval_seconds == 90
        # Route table is reflected verbatim
        assert (
            cfg.feishu_channel_routes["planning-agent"] == "aihubmix/gemini-3.5-flash"
        )
        assert (
            cfg.feishu_channel_routes["code-agent"]["provider_id"]
            == "codex/gpt-5.5-high"
        )
