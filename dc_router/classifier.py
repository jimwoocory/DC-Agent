"""Optional LLM classifier contract for uncertain routing cases."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from dc_router.provider_map import GEMINI_3_1_PRO
from dc_router.taxonomy import RouterIntent

ROUTER_CLASSIFIER_PROVIDER_ID = GEMINI_3_1_PRO

ROUTER_CLASSIFIER_SYSTEM_PROMPT = """\
You are the DC Router classifier. Return one JSON object only.
Choose exactly one intent from:
casual, ops_writing, realtime, public_opinion, simple_code,
creative, insight, deep_creative, deep_insight, fallback.
Do not answer the user's task.

JSON schema:
{"intent":"casual|ops_writing|realtime|public_opinion|simple_code|creative|insight|deep_creative|deep_insight|fallback","confidence":0.0,"reason":"short reason"}
"""


@dataclass(frozen=True, slots=True)
class ClassifierResult:
    intent: RouterIntent
    confidence: float
    reason: str


class RouterClassifier(Protocol):
    async def classify(self, text: str) -> ClassifierResult | None:
        """Classify uncertain text, or return None to let fallback handle it."""


class NoopRouterClassifier:
    async def classify(self, text: str) -> ClassifierResult | None:
        return None
