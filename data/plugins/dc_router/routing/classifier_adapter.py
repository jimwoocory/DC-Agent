"""Standalone LLM classifier adapter for uncertain routing cases.

Extracted from routing_adapter.py (legacy 2600-line compat module) to break
the circular dependency between dispatch.py → routing_adapter.py.

The classifier calls a fixed model (aihubmix Gemini 3.1 Pro) to classify
messages that don't match any deterministic rule.

Usage:
    from .classifier_adapter import AstrBotRouterClassifier, parse_classifier_json
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from astrbot.api import logger

# ─── JSON extraction regex ────────────────────────────────────────────────
# 1) code-block wrapped: ```json {"intent": ...} ```
_CLASSIFIER_JSON_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
# 2) bare JSON with "intent" key
_CLASSIFIER_JSON_BARE_RE = re.compile(r'\{[^{}]*"intent"[^{}]*\}', re.DOTALL)

# Timeout for a single LLM classifier call (seconds).
_CLASSIFIER_CALL_TIMEOUT: float = 15.0


def parse_classifier_json(raw: str) -> dict | None:
    """Extract classifier JSON from LLM output.

    Handles three formats:
    1. Code-block wrapped: ```json {"intent": ...} ```
    2. Bare JSON: {"intent": "creative", ...}
    3. JSON with surrounding text: Here is the result: {"intent": ...}

    Returns None when no valid JSON object with an "intent" key is found.
    """
    if not raw:
        return None
    # Try code-block wrapped first
    match = _CLASSIFIER_JSON_RE.search(raw)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    # Try bare JSON with "intent" key
    match = _CLASSIFIER_JSON_BARE_RE.search(raw)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    # Last resort: find any JSON-like block with intent
    for m in re.finditer(r"\{[^{}]*\}", raw):
        try:
            data = json.loads(m.group(0))
            if isinstance(data, dict) and "intent" in data:
                return data
        except json.JSONDecodeError:
            continue
    return None


def _get_provider_by_id(context: Any, provider_id: str) -> Any:
    """Best-effort provider lookup via AstrBot plugin context."""
    getter = getattr(context, "get_provider_by_id", None)
    if not callable(getter):
        return None
    try:
        return getter(provider_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[dc-router] get_provider_by_id(%s) 失败: %s", provider_id, exc)
        return None


class AstrBotRouterClassifier:
    """Router LLM classifier backed by aihubmix Gemini 3.1 Pro provider.

    Uses canonical dc_router_core imports (not the dc_router shim) to avoid
    ModuleNotFoundError regressions (see test_router_core.py:6-11).
    """

    def __init__(self, context: Any) -> None:
        self.context = context

    async def classify(self, text: str) -> Any:
        from dc_router_core.classifier import (
            ROUTER_CLASSIFIER_PROVIDER_ID,
            ROUTER_CLASSIFIER_SYSTEM_PROMPT,
            ClassifierResult,
        )
        from dc_router_core.taxonomy import RouterIntent

        provider = _get_provider_by_id(self.context, ROUTER_CLASSIFIER_PROVIDER_ID)
        if provider is None:
            logger.debug(
                "[dc-router] classifier provider %s 不存在，跳过 LLM 辅助裁判",
                ROUTER_CLASSIFIER_PROVIDER_ID,
            )
            return None

        try:
            resp = await asyncio.wait_for(
                provider.text_chat(
                    prompt=text[:2000],
                    system_prompt=ROUTER_CLASSIFIER_SYSTEM_PROMPT,
                    contexts=[],
                ),
                timeout=_CLASSIFIER_CALL_TIMEOUT,
            )
            raw = (getattr(resp, "completion_text", "") or "").strip()
            data = parse_classifier_json(raw)
            if data is None:
                logger.debug("[dc-router] classifier 输出无有效 JSON: %r", raw[:120])
                return None
            intent_str = str(data.get("intent", "")).strip()
            try:
                intent = RouterIntent(intent_str)
            except ValueError:
                logger.warning(
                    "[dc-router] classifier 返回未知 intent=%r，回退 fallback",
                    intent_str,
                )
                intent = RouterIntent.FALLBACK
            confidence = float(data.get("confidence", 0.5) or 0.5)
            reason = str(data.get("reason", "classifier match")).strip()
            return ClassifierResult(
                intent=intent,
                confidence=max(0.0, min(confidence, 1.0)),
                reason=reason,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "[dc-router] classifier 超时 (%.0fs)，跳过 LLM 辅助裁判",
                _CLASSIFIER_CALL_TIMEOUT,
            )
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning("[dc-router] classifier 调用失败，跳过: %s", exc)
            return None


__all__ = [
    "AstrBotRouterClassifier",
    "parse_classifier_json",
]
