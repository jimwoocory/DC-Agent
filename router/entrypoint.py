"""Single-entry router facade for the new DC path.

按 envelope.metadata['platform_id'] 自动切 business / ops:
- "巅池-技术（DevOps）" → ops 路由表 (router/ops_*.py)
- 其他 platform_id  → business 路由表 (router/taxonomy.py 等)
参考 memory: project-dual-bot-router-architecture
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

from router.classifier import NoopRouterClassifier, RouterClassifier
from router.content_sop import infer_content_sop_metadata
from router.decision import RouterDecision
from router.ops_provider_map import get_ops_provider_route
from router.ops_rules import OpsRuleMatch, match_ops_keywords, match_ops_prefix
from router.ops_taxonomy import OpsIntent
from router.provider_map import get_provider_route
from router.rules import RuleMatch, match_document_link, match_keywords, match_prefix
from router.taxonomy import AttachmentKind, RouterIntent


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


class DCRouter:
    # DevOps 入口走独立 ops 路由表；旧「巅池-技术」如果被 silent_observer
    # 提前 stop_event，通常不会到这里。
    OPS_PLATFORM_IDS: ClassVar[set[str]] = {"巅池-技术（DevOps）", "巅池-技术"}

    def __init__(self, classifier: RouterClassifier | None = None) -> None:
        self.classifier = classifier or NoopRouterClassifier()

    async def decide(self, message: MessageEnvelope | str) -> RouterDecision:
        envelope = self._coerce_message(message)
        platform_id = envelope.metadata.get("platform_id", "")

        # ─── 按 platform_id 切两套路由表（business / ops 完全独立）───
        if platform_id in self.OPS_PLATFORM_IDS:
            return self._decide_ops(envelope)
        return await self._decide_business(envelope)

    # ─────────────────── business 路由（员工业务入口）───────────────────

    async def _decide_business(self, envelope: MessageEnvelope) -> RouterDecision:
        forced_match = match_prefix(envelope.text)

        if envelope.has_attachments and not envelope.attachment_summary:
            return self._decision_for(
                RuleMatch(
                    intent=RouterIntent.MULTIMODAL,
                    reason="Attachment requires aihubmix Gemini Flash preprocessing.",
                    source="attachment",
                ),
                needs_multimodal_preprocess=True,
                metadata=self._metadata(envelope, forced_match=forced_match),
            )

        if forced_match:
            return self._decision_for(
                forced_match,
                metadata=self._metadata(envelope, forced_match=forced_match),
            )

        document_link_match = match_document_link(envelope.text)
        if document_link_match:
            return self._decision_for(
                document_link_match,
                metadata=self._metadata(envelope),
            )

        keyword_match = match_keywords(envelope.combined_text)
        if keyword_match and keyword_match.intent in {
            RouterIntent.PUBLIC_OPINION,
            RouterIntent.DEEP_CREATIVE,
            RouterIntent.DEEP_INSIGHT,
            RouterIntent.SIMPLE_CODE,
            RouterIntent.REALTIME,
        }:
            return self._decision_for(
                keyword_match,
                metadata=self._metadata(envelope),
            )

        content_metadata = self._metadata(envelope)
        if content_metadata.get("content_sop") == "true":
            intent = (
                RouterIntent.DEEP_CREATIVE
                if content_metadata.get("content_type") == "mixed"
                else RouterIntent.CREATIVE
            )
            return self._decision_for(
                RuleMatch(
                    intent=intent,
                    reason="Matched deterministic content SOP metadata.",
                    source="content_sop",
                ),
                metadata=content_metadata,
            )

        if keyword_match:
            return self._decision_for(
                keyword_match,
                metadata=self._metadata(envelope),
            )

        classifier_result = await self.classifier.classify(envelope.combined_text)
        if classifier_result:
            return self._decision_for(
                RuleMatch(
                    intent=classifier_result.intent,
                    reason=classifier_result.reason,
                    source="classifier",
                ),
                metadata={
                    **self._metadata(envelope),
                    "classifier_confidence": str(classifier_result.confidence),
                },
            )

        return self._decision_for(
            RuleMatch(
                intent=RouterIntent.FALLBACK,
                reason="No prefix, attachment, keyword, or classifier match.",
                source="fallback",
            ),
            metadata=self._metadata(envelope),
        )

    # ─────────────────── ops 路由（DevOps 机器人入口）───────────────────

    def _decide_ops(self, envelope: MessageEnvelope) -> RouterDecision:
        """运维场景: 关键词/前缀够用, 不调 classifier, 不接 attachment 多模态。"""
        # ops 前缀
        forced = match_ops_prefix(envelope.text)
        if forced:
            return self._decision_for_ops(forced, envelope)

        # ops 关键词
        keyword = match_ops_keywords(envelope.combined_text)
        if keyword:
            return self._decision_for_ops(keyword, envelope)

        # fallback (Codex CLI gpt-5.4)
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
        metadata = self._metadata(envelope)
        metadata["router_mode"] = "ops"
        return RouterDecision.from_ops_route(
            route,
            reason=match.reason,
            source=match.source,
            metadata=metadata,
        )

    def _decision_for(
        self,
        match: RuleMatch,
        *,
        needs_multimodal_preprocess: bool = False,
        metadata: dict[str, str] | None = None,
    ) -> RouterDecision:
        route = get_provider_route(match.intent)
        return RouterDecision.from_route(
            route,
            reason=match.reason,
            source=match.source,
            needs_multimodal_preprocess=needs_multimodal_preprocess,
            metadata=metadata,
        )

    def _coerce_message(self, message: MessageEnvelope | str) -> MessageEnvelope:
        if isinstance(message, MessageEnvelope):
            return message
        return MessageEnvelope(text=message)

    def _metadata(
        self,
        envelope: MessageEnvelope,
        *,
        forced_match: RuleMatch | None = None,
    ) -> dict[str, str]:
        metadata = dict(envelope.metadata)
        metadata.update(
            infer_content_sop_metadata(
                envelope.text,
                attachment_summary=envelope.attachment_summary,
                has_attachments=envelope.has_attachments,
            ).to_metadata()
        )
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
