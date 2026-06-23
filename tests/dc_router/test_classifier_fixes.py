"""Tests for classifier JSON parsing robustness and config integration.

Covers:
- _parse_classifier_json: code-block, bare, surrounding text, empty, malformed
- DCRouterConfig.classifier_enabled field
- _build_classifier injection into dispatch pipeline
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure project root is on sys.path for dc_router_core imports.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ─────────────────────── _parse_classifier_json ───────────────────────


class TestParseClassifierJson:
    """Verify the three-format JSON extraction used by AstrBotRouterClassifier."""

    def _parse(self, raw: str) -> dict | None:
        # Import from the canonical module (routing/classifier_adapter.py).
        from data.plugins.dc_router.routing.classifier_adapter import (
            parse_classifier_json,
        )

        return parse_classifier_json(raw)

    def test_bare_json(self) -> None:
        raw = '{"intent": "creative", "confidence": 0.85, "reason": "test"}'
        result = self._parse(raw)
        assert result is not None
        assert result["intent"] == "creative"
        assert result["confidence"] == 0.85

    def test_code_block_wrapped(self) -> None:
        raw = '```json\n{"intent": "realtime", "confidence": 0.9, "reason": "search"}\n```'
        result = self._parse(raw)
        assert result is not None
        assert result["intent"] == "realtime"

    def test_code_block_without_language_tag(self) -> None:
        raw = '```\n{"intent": "casual", "confidence": 0.7, "reason": "chat"}\n```'
        result = self._parse(raw)
        assert result is not None
        assert result["intent"] == "casual"

    def test_json_with_surrounding_text(self) -> None:
        raw = 'Here is the classification result:\n{"intent": "deep_insight", "confidence": 0.92, "reason": "PRD task"}\nDone.'
        result = self._parse(raw)
        assert result is not None
        assert result["intent"] == "deep_insight"

    def test_empty_string(self) -> None:
        assert self._parse("") is None

    def test_no_json_at_all(self) -> None:
        assert self._parse("Sorry, I cannot classify this message.") is None

    def test_malformed_json(self) -> None:
        assert self._parse('{"intent": "creative", "confidence": }') is None

    def test_json_without_intent_key(self) -> None:
        raw = '{"category": "creative", "score": 0.8}'
        assert self._parse(raw) is None

    def test_json_with_extra_fields(self) -> None:
        raw = '{"intent": "insight", "confidence": 0.88, "reason": "brand analysis", "extra": "ignored"}'
        result = self._parse(raw)
        assert result is not None
        assert result["intent"] == "insight"
        assert result["confidence"] == 0.88


# ─────────────────────── DCRouterConfig.classifier_enabled ───────────────────────


class TestConfigClassifierEnabled:
    """Verify the classifier_enabled config field in DCRouterConfig."""

    def test_default_is_false(self) -> None:
        """classifier_enabled defaults to False to avoid unexpected LLM calls."""
        from data.plugins.dc_router.config import DCRouterConfig

        cfg = DCRouterConfig()
        assert cfg.classifier_enabled is False

    def test_load_config_reads_classifier_enabled(self, tmp_path: Path) -> None:
        from data.plugins.dc_router.config import load_config

        cfg_file = tmp_path / "dc_router_config.json"
        cfg_file.write_text(
            json.dumps(
                {"enabled": True, "dry_run": False, "classifier_enabled": False}
            ),
            encoding="utf-8",
        )
        cfg = load_config(cfg_file)
        assert cfg.classifier_enabled is False
        assert cfg.enabled is True

    def test_load_config_defaults_classifier_enabled_to_false(
        self, tmp_path: Path
    ) -> None:
        from data.plugins.dc_router.config import load_config

        cfg_file = tmp_path / "dc_router_config.json"
        cfg_file.write_text(
            json.dumps({"enabled": True, "dry_run": False}),
            encoding="utf-8",
        )
        cfg = load_config(cfg_file)
        assert cfg.classifier_enabled is False

    def test_load_config_missing_file_defaults_false(self, tmp_path: Path) -> None:
        from data.plugins.dc_router.config import load_config

        cfg = load_config(tmp_path / "nonexistent.json")
        assert cfg.classifier_enabled is False

    def test_load_config_can_enable_classifier(self, tmp_path: Path) -> None:
        from data.plugins.dc_router.config import load_config

        cfg_file = tmp_path / "dc_router_config.json"
        cfg_file.write_text(
            json.dumps({"classifier_enabled": True}),
            encoding="utf-8",
        )
        cfg = load_config(cfg_file)
        assert cfg.classifier_enabled is True


# ─────────────────────── _build_classifier in dispatch ───────────────────────


class TestBuildClassifier:
    """Verify _build_classifier constructs classifier when enabled."""

    def test_returns_none_when_disabled(self) -> None:
        from data.plugins.dc_router.config import DCRouterConfig
        from data.plugins.dc_router.dispatch import _build_classifier

        cfg = DCRouterConfig(classifier_enabled=False)
        result = _build_classifier(MagicMock(), cfg)
        assert result is None

    def test_returns_classifier_when_enabled(self) -> None:
        from data.plugins.dc_router.config import DCRouterConfig
        from data.plugins.dc_router.dispatch import _build_classifier

        cfg = DCRouterConfig(classifier_enabled=True)
        ctx = MagicMock()
        result = _build_classifier(ctx, cfg)
        # AstrBotRouterClassifier should be constructed successfully
        assert result is not None
        assert hasattr(result, "classify")

    def test_returns_none_on_import_failure(self) -> None:
        from data.plugins.dc_router.config import DCRouterConfig
        from data.plugins.dc_router.dispatch import _build_classifier

        cfg = DCRouterConfig(classifier_enabled=True)
        ctx = MagicMock()
        # Simulate import failure by patching the new canonical module path
        with patch.dict(
            "sys.modules",
            {"data.plugins.dc_router.routing.classifier_adapter": None},
        ):
            result = _build_classifier(ctx, cfg)
        # Should gracefully return None on failure
        assert result is None


# ─────────────────────── Classifier integration in _maybe_classify ───────────────────────


class TestMaybeClassifyResilience:
    """Verify _maybe_classify handles various failure modes gracefully."""

    @pytest.mark.asyncio
    async def test_classifier_returns_none_keeps_fallback(self) -> None:
        from dc_router_core.classifier import ClassifierResult
        from dc_router_core.entrypoint import DCRouter

        class NoneClassifier:
            async def classify(self, text: str) -> ClassifierResult | None:
                return None

        dc_router = DCRouter(classifier=NoneClassifier())
        decision = await dc_router.decide("random text without keywords")
        assert decision.source == "fallback"

    @pytest.mark.asyncio
    async def test_classifier_low_confidence_rejected_by_threshold(self) -> None:
        """Below CLASSIFIER_CONFIDENCE_THRESHOLD (0.65), stays as FALLBACK."""
        from dc_router_core.classifier import ClassifierResult
        from dc_router_core.entrypoint import DCRouter
        from dc_router_core.taxonomy import RouterIntent

        class LowConfClassifier:
            async def classify(self, text: str) -> ClassifierResult | None:
                return ClassifierResult(
                    intent=RouterIntent.CREATIVE,
                    confidence=0.3,
                    reason="low confidence guess",
                )

        dc_router = DCRouter(classifier=LowConfClassifier())
        decision = await dc_router.decide("给这个活动想几个方向吧")
        # Low confidence → stays FALLBACK
        assert decision.source == "fallback"
        assert decision.intent == RouterIntent.FALLBACK.value

    @pytest.mark.asyncio
    async def test_classifier_high_confidence_accepted(self) -> None:
        """Above CLASSIFIER_CONFIDENCE_THRESHOLD (0.65), routes via classifier."""
        from dc_router_core.classifier import ClassifierResult
        from dc_router_core.entrypoint import DCRouter
        from dc_router_core.taxonomy import RouterIntent

        class HighConfClassifier:
            async def classify(self, text: str) -> ClassifierResult | None:
                return ClassifierResult(
                    intent=RouterIntent.CREATIVE,
                    confidence=0.87,
                    reason="high confidence creative",
                )

        dc_router = DCRouter(classifier=HighConfClassifier())
        decision = await dc_router.decide("给这个活动想几个方向吧")
        assert decision.source == "classifier"
        assert decision.intent == RouterIntent.CREATIVE.value
        assert decision.metadata["classifier_confidence"] == "0.87"

    @pytest.mark.asyncio
    async def test_rules_match_skips_classifier(self) -> None:
        from dc_router_core.classifier import ClassifierResult
        from dc_router_core.entrypoint import DCRouter
        from dc_router_core.taxonomy import RouterIntent

        class ShouldNotCallClassifier:
            async def classify(self, text: str) -> ClassifierResult | None:
                msg = "classifier should not be called when rules match"
                raise AssertionError(msg)

        dc_router = DCRouter(classifier=ShouldNotCallClassifier())
        # Prefix rule should match immediately, never calling classifier
        decision = await dc_router.decide("#创意 帮我写个 slogan")
        assert decision.source == "prefix"
        assert decision.intent == RouterIntent.CREATIVE.value
