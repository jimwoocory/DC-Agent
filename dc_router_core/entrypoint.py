"""Single-entry router facade for the new DC path.

按 envelope.metadata['platform_id'] 自动切 business / ops:
- "巅池-技术（DevOps）" → ops 路由表 (dc_router_core/ops_*.py)
- 其他 platform_id  → business 路由表 (dc_router_core/taxonomy.py 等)
参考 memory: project-dual-bot-router-architecture

三层架构（2026-06-09 合并后）：
- L1 意图判定（Intent Resolver）：prefix / attachment / keyword / sop / classifier
- L2 路由解析（Route Resolver）：RouterIntent → ProviderRoute
- L3 仲裁（Arbiter）：quota / circuit / depth 升级
任何下游（data/plugins/dc_router/、harness、卡片渲染）只调用 DCRouter.decide()。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import ClassVar, Protocol

from dc_router_core.classifier import NoopRouterClassifier, RouterClassifier
from dc_router_core.content_sop import infer_content_sop_metadata
from dc_router_core.decision import RouterDecision
from dc_router_core.decision_framework import build_decision_context
from dc_router_core.department_requirements import match_department_requirement
from dc_router_core.ops_provider_map import get_ops_provider_route
from dc_router_core.ops_rules import OpsRuleMatch, match_ops_keywords, match_ops_prefix
from dc_router_core.ops_taxonomy import OpsIntent
from dc_router_core.provider_map import get_provider_route
from dc_router_core.rules import (
    RuleMatch,
    match_document_link,
    match_keywords,
    match_prefix,
)
from dc_router_core.taxonomy import (
    AttachmentKind,
    RouteAction,
    RouteDepth,
    RouterIntent,
)

_log = logging.getLogger(__name__)

# Maximum seconds to wait for the LLM classifier before falling back to rules-only.
CLASSIFIER_TIMEOUT_SECONDS: float = 12.0

# Minimum confidence for classifier to accept a routing decision.
# Below this threshold, the message stays as FALLBACK (rules-only).
CLASSIFIER_CONFIDENCE_THRESHOLD: float = 0.65


@dataclass(slots=True)
class MessageEnvelope:
    text: str
    attachment_kinds: tuple[AttachmentKind | str, ...] = ()
    attachment_summary: str | None = None
    user_id: str | None = None
    session_id: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def has_attachments(self) -> bool:
        return bool(self.attachment_kinds)

    @property
    def combined_text(self) -> str:
        if not self.attachment_summary:
            return self.text
        return f"{self.text}\n\n[attachment_summary]\n{self.attachment_summary}"


# ───────── L3 仲裁接口（真实实现见 harness/route_arbiter.QuotaGateArbiter） ─────────
class RouteArbiter(Protocol):
    async def arbitrate(
        self,
        route,
        envelope: MessageEnvelope,
        metadata: dict[str, str],
    ) -> ArbitrationResult: ...


@dataclass(slots=True)
class ArbitrationResult:
    route: object
    action: RouteAction
    depth: RouteDepth
    metadata: dict[str, str]
    reason: str = ""
    source: str = "arbiter"


class PassThroughArbiter:
    """默认仲裁器：不改 ProviderRoute，只透传。

    真实实现是 harness/route_arbiter.QuotaGateArbiter：
    - circuit open → 改 fallback provider
    - quota exhausted → 轻意图升级 depth=FRONT 排队，重意图升级 depth=HERMES
    """

    async def arbitrate(
        self,
        route,
        envelope: MessageEnvelope,
        metadata: dict[str, str],
    ) -> ArbitrationResult:
        return ArbitrationResult(
            route=route,
            action=getattr(route, "action", RouteAction.ANSWER),
            depth=getattr(route, "depth", RouteDepth.DIRECT),
            metadata=metadata,
            reason="pass-through",
            source="arbiter",
        )


class DCRouter:
    OPS_PLATFORM_IDS: ClassVar[set[str]] = {"巅池-技术（DevOps）", "巅池-技术"}

    def __init__(
        self,
        classifier: RouterClassifier | None = None,
        arbiter: RouteArbiter | None = None,
    ) -> None:
        self.classifier = classifier or NoopRouterClassifier()
        self.arbiter = arbiter or PassThroughArbiter()

    async def decide(self, message: MessageEnvelope | str) -> RouterDecision:
        envelope = self._coerce_message(message)
        platform_id = envelope.metadata.get("platform_id", "")

        if platform_id in self.OPS_PLATFORM_IDS:
            return self._decide_ops(envelope)
        return await self._decide_business(envelope)

    # ───────────────── L1 意图判定 ─────────────────

    def _resolve_intent(self, envelope: MessageEnvelope) -> RuleMatch:
        # 顺序敏感：先强信号，再 sop，再弱信号，再 fallback。
        if envelope.has_attachments and not envelope.attachment_summary:
            return RuleMatch(
                intent=RouterIntent.MULTIMODAL,
                reason="Attachment requires aihubmix Gemini Flash preprocessing.",
                source="attachment",
            )

        forced = match_prefix(envelope.text)
        if forced:
            return forced

        doc_link = match_document_link(envelope.text)
        if doc_link:
            return doc_link

        keyword = match_keywords(envelope.combined_text)
        if keyword and keyword.intent in {
            RouterIntent.DEEP_CREATIVE,
            RouterIntent.DEEP_INSIGHT,
            RouterIntent.SIMPLE_CODE,
        }:
            return keyword

        department_requirement = match_department_requirement(
            envelope.combined_text,
            metadata=envelope.metadata,
        )
        if department_requirement:
            return RuleMatch(
                intent=department_requirement.intent,
                reason=department_requirement.reason,
                source="department_workflow",
            )

        if keyword and keyword.intent in {
            RouterIntent.PUBLIC_OPINION,
            RouterIntent.REALTIME,
        }:
            return keyword

        if keyword and keyword.intent == RouterIntent.OPS_WRITING:
            return keyword

        content_metadata = self._build_metadata(envelope)
        if content_metadata.get("content_sop") == "true":
            intent = (
                RouterIntent.DEEP_CREATIVE
                if content_metadata.get("content_type") == "mixed"
                else RouterIntent.CREATIVE
            )
            return RuleMatch(
                intent=intent,
                reason="Matched deterministic content SOP metadata.",
                source="content_sop",
            )

        if keyword:
            return keyword

        return RuleMatch(
            intent=RouterIntent.FALLBACK,
            reason="No prefix, attachment, keyword, sop, or classifier match.",
            source="fallback",
        )

    async def _maybe_classify(
        self,
        envelope: MessageEnvelope,
        primary: RuleMatch,
    ) -> RuleMatch:
        if primary.source != "fallback":
            return primary
        if primary.intent != RouterIntent.FALLBACK:
            return primary
        try:
            classifier_result = await asyncio.wait_for(
                self.classifier.classify(envelope.combined_text),
                timeout=CLASSIFIER_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            _log.warning(
                "[dc_router] classifier timed out after %.1fs, falling back to rules",
                CLASSIFIER_TIMEOUT_SECONDS,
            )
            return primary
        except Exception:  # noqa: BLE001
            _log.warning(
                "[dc_router] classifier raised unexpected error, falling back to rules",
                exc_info=True,
            )
            return primary
        if classifier_result is None:
            return primary
        confidence = float(getattr(classifier_result, "confidence", 0) or 0)
        if confidence < CLASSIFIER_CONFIDENCE_THRESHOLD:
            _log.info(
                "[dc_router] classifier confidence %.2f below threshold %.2f, "
                "keeping FALLBACK (intent=%s)",
                confidence,
                CLASSIFIER_CONFIDENCE_THRESHOLD,
                classifier_result.intent.value,
            )
            return primary
        return RuleMatch(
            intent=classifier_result.intent,
            reason=classifier_result.reason,
            source="classifier",
            confidence=str(confidence),
        )

    # ───────────────── L2 路由解析 ─────────────────

    def _resolve_route(self, match: RuleMatch):
        # Returns ProviderRoute; arbiter reads it at L3
        route = get_provider_route(match.intent)
        return route

    # ───────────────── 业务入口（L1→L2→L3）─────────────────

    async def _decide_business(self, envelope: MessageEnvelope) -> RouterDecision:
        forced_match = match_prefix(envelope.text)
        primary = self._resolve_intent(envelope)
        resolved = await self._maybe_classify(envelope, primary)

        metadata = self._build_metadata(envelope, forced_match=forced_match)
        if resolved.source == "classifier" and resolved.confidence:
            metadata["classifier_confidence"] = resolved.confidence
        route = self._resolve_route(resolved)
        arbitration = await self.arbiter.arbitrate(route, envelope, metadata)
        final_action = (
            RouteAction.ANSWER
            if resolved.intent != RouterIntent.MULTIMODAL
            else RouteAction.PREPROCESS
        )

        decision = RouterDecision.from_route(
            arbitration.route,
            reason=resolved.reason,
            source=resolved.source,
            needs_multimodal_preprocess=final_action == RouteAction.PREPROCESS,
            metadata=arbitration.metadata,
        )
        # 仲裁结果覆盖 depth/action（仲裁器未改动时与 route 一致，等价透传）
        arbitrated_depth = getattr(arbitration, "depth", decision.depth)
        arbitrated_action = getattr(arbitration, "action", decision.action)
        if arbitrated_depth != decision.depth or arbitrated_action != decision.action:
            decision = decision.model_copy(
                update={"depth": arbitrated_depth, "action": arbitrated_action}
            )
            if arbitration.reason and arbitration.reason != "pass-through":
                decision.metadata["arbiter_reason"] = arbitration.reason
        return decision

    # ───────────────── ops 入口（保持现状，quota/circuit 改造由 harness 接管） ─────────────────

    def _decide_ops(self, envelope: MessageEnvelope) -> RouterDecision:
        forced = match_ops_prefix(envelope.text)
        if forced:
            return self._decision_for_ops(forced, envelope)
        keyword = match_ops_keywords(envelope.combined_text)
        if keyword:
            return self._decision_for_ops(keyword, envelope)
        return self._decision_for_ops(
            OpsRuleMatch(
                intent=OpsIntent.OPS_FALLBACK,
                reason="No ops prefix or keyword match.",
                source="fallback",
            ),
            envelope,
        )

    def _decision_for_ops(
        self,
        match: OpsRuleMatch,
        envelope: MessageEnvelope,
    ) -> RouterDecision:
        route = get_ops_provider_route(match.intent)
        metadata = self._build_metadata(envelope)
        metadata["router_mode"] = "ops"
        return RouterDecision.from_ops_route(
            route,
            reason=match.reason,
            source=match.source,
            metadata=metadata,
        )

    def _coerce_message(self, message: MessageEnvelope | str) -> MessageEnvelope:
        if isinstance(message, MessageEnvelope):
            return message
        return MessageEnvelope(text=message)

    def _build_metadata(
        self,
        envelope: MessageEnvelope,
        *,
        forced_match: RuleMatch | None = None,
    ) -> dict[str, str]:
        metadata = dict(envelope.metadata)
        metadata.update(
            build_decision_context(
                envelope.text,
                metadata=metadata,
                attachment_kinds=tuple(
                    str(kind.value if isinstance(kind, AttachmentKind) else kind)
                    for kind in envelope.attachment_kinds
                ),
                attachment_summary=envelope.attachment_summary,
            ).to_metadata()
        )
        metadata.update(
            infer_content_sop_metadata(
                envelope.text,
                attachment_summary=envelope.attachment_summary,
                has_attachments=envelope.has_attachments,
            ).to_metadata()
        )
        department_requirement = match_department_requirement(
            envelope.combined_text,
            metadata=metadata,
        )
        if department_requirement:
            metadata.update(department_requirement.metadata)
        if envelope.user_id:
            metadata["user_id"] = envelope.user_id
        if envelope.session_id:
            metadata["session_id"] = envelope.session_id
        if envelope.attachment_kinds:
            metadata["attachment_kinds"] = ",".join(
                str(kind.value if isinstance(kind, AttachmentKind) else kind)
                for kind in envelope.attachment_kinds
            )
        if forced_match:
            metadata["forced_intent"] = forced_match.intent.value
        return metadata
