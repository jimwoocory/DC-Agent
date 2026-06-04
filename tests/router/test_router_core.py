from __future__ import annotations

import pytest

from router.classifier import ClassifierResult
from router.decision import RouterDecision
from router.entrypoint import DCRouter, MessageEnvelope
from router.ops_taxonomy import OpsIntent
from router.provider_map import get_provider_route
from router.rules import match_document_link, match_keywords, match_prefix
from router.taxonomy import AttachmentKind, RouterIntent


@pytest.mark.parametrize(
    ("text", "intent"),
    [
        ("#深度 做一份品牌分析", RouterIntent.DEEP_INSIGHT),
        ("#PRD 写一个后台需求", RouterIntent.DEEP_INSIGHT),
        ("#prd 写一个后台需求", RouterIntent.DEEP_INSIGHT),
        ("#洞察 这类用户怎么想", RouterIntent.INSIGHT),
        ("#创意 给我 10 个 slogan", RouterIntent.CREATIVE),
        ("#舆情 负面评论怎么回", RouterIntent.PUBLIC_OPINION),
        ("#代码 解释这个 traceback", RouterIntent.SIMPLE_CODE),
    ],
)
def test_prefix_rules_route_to_expected_intents(
    text: str,
    intent: RouterIntent,
) -> None:
    match = match_prefix(text)

    assert match is not None
    assert match.intent is intent
    assert match.source == "prefix"


def test_public_opinion_keyword_has_priority_over_realtime_and_creative() -> None:
    match = match_keywords("最新热点舆情来了，顺便写一版营销文案")

    assert match is not None
    assert match.intent is RouterIntent.PUBLIC_OPINION


def test_deep_creative_keyword_has_priority_over_creative() -> None:
    match = match_keywords("做一份完整营销方案，里面也包含 slogan")

    assert match is not None
    assert match.intent is RouterIntent.DEEP_CREATIVE


def test_explicit_prd_task_routes_to_deep_insight() -> None:
    match = match_keywords("帮我起草一个 PRD 文档，包含功能模块和验收标准")

    assert match is not None
    assert match.intent is RouterIntent.DEEP_INSIGHT


def test_feishu_document_link_routes_to_multimodal_preprocess() -> None:
    match = match_document_link("看一下 https://demo.feishu.cn/docx/Abc_123")

    assert match is not None
    assert match.intent is RouterIntent.MULTIMODAL
    assert match.source == "document_link"


def test_router_intent_enum_is_explicit_and_complete() -> None:
    assert {intent.value for intent in RouterIntent} == {
        "casual",
        "ops_writing",
        "multimodal",
        "realtime",
        "public_opinion",
        "simple_code",
        "creative",
        "insight",
        "deep_creative",
        "deep_insight",
        "fallback",
    }


def test_router_decision_from_route_preserves_provider_contract() -> None:
    route = get_provider_route(RouterIntent.DEEP_INSIGHT)
    decision = RouterDecision.from_route(
        route,
        reason="unit test",
        source="rules",
        metadata={"session_id": "session-1"},
    )

    assert decision.intent == RouterIntent.DEEP_INSIGHT.value
    assert decision.provider_id == route.provider_id
    assert decision.target_model == route.target_model
    assert decision.metadata == {"session_id": "session-1"}


class FakeClassifier:
    def __init__(self, result: ClassifierResult | None) -> None:
        self.result = result
        self.seen_text = ""

    async def classify(self, text: str) -> ClassifierResult | None:
        self.seen_text = text
        return self.result


class ExplodingClassifier:
    async def classify(self, text: str) -> ClassifierResult | None:
        msg = f"classifier should not be called for: {text}"
        raise AssertionError(msg)


@pytest.mark.asyncio
async def test_business_router_uses_classifier_when_rules_are_uncertain() -> None:
    classifier = FakeClassifier(
        ClassifierResult(
            intent=RouterIntent.CREATIVE,
            confidence=0.87,
            reason="mock creative",
        )
    )
    router = DCRouter(classifier=classifier)

    decision = await router.decide(
        MessageEnvelope(
            text="给这个活动想几个方向",
            attachment_summary="目标用户是年轻家庭",
            session_id="session-1",
        )
    )

    assert decision.intent == RouterIntent.CREATIVE.value
    assert decision.source == "classifier"
    assert decision.metadata["classifier_confidence"] == "0.87"
    assert "年轻家庭" in classifier.seen_text
    assert decision.metadata["session_id"] == "session-1"


@pytest.mark.asyncio
async def test_business_router_falls_back_when_classifier_returns_none() -> None:
    router = DCRouter(classifier=FakeClassifier(None))

    decision = await router.decide("这个事你怎么看")

    assert decision.intent == RouterIntent.FALLBACK.value
    assert decision.source == "fallback"


@pytest.mark.asyncio
async def test_attachment_without_summary_requires_multimodal_preprocess() -> None:
    router = DCRouter(classifier=ExplodingClassifier())

    decision = await router.decide(
        MessageEnvelope(
            text="看这张图",
            attachment_kinds=(AttachmentKind.IMAGE,),
            user_id="ou-user",
        )
    )

    assert decision.intent == RouterIntent.MULTIMODAL.value
    assert decision.needs_multimodal_preprocess is True
    assert decision.metadata["attachment_kinds"] == "image"
    assert decision.metadata["user_id"] == "ou-user"


@pytest.mark.asyncio
async def test_prefix_overrides_keyword_after_attachment_summary_exists() -> None:
    router = DCRouter(classifier=ExplodingClassifier())

    decision = await router.decide(
        MessageEnvelope(
            text="#创意 这个舆情危机怎么回应",
            attachment_summary="截图里是负面评论",
        )
    )

    assert decision.intent == RouterIntent.CREATIVE.value
    assert decision.source == "prefix"
    assert decision.metadata["forced_intent"] == RouterIntent.CREATIVE.value


@pytest.mark.asyncio
async def test_ops_platform_uses_ops_router_without_classifier() -> None:
    router = DCRouter(classifier=ExplodingClassifier())

    decision = await router.decide(
        MessageEnvelope(
            text="#队列 看一下失败任务",
            metadata={"platform_id": "巅池-技术（DevOps）"},
        )
    )

    assert decision.intent == OpsIntent.QUEUE_STATUS.value
    assert decision.provider_id == "cli/codex/gpt-5.4"
    assert decision.metadata["router_mode"] == "ops"


@pytest.mark.asyncio
async def test_ops_keyword_priority_prefers_quota_gate_view() -> None:
    router = DCRouter(classifier=ExplodingClassifier())

    decision = await router.decide(
        MessageEnvelope(
            text="队列和凭证池现在怎么样",
            metadata={"platform_id": "巅池-技术"},
        )
    )

    assert decision.intent == OpsIntent.QUOTA_GATE_VIEW.value
    assert decision.source == "keyword"


@pytest.mark.asyncio
async def test_content_sop_metadata_routes_client_copy_image_request() -> None:
    router = DCRouter(classifier=ExplodingClassifier())

    decision = await router.decide("帮我写客户邀约文案并配图，用在私域活动触达")

    assert decision.intent == RouterIntent.DEEP_CREATIVE.value
    assert decision.source == "content_sop"
    assert decision.metadata["content_sop"] == "true"
    assert decision.metadata["department"] == "client_dept"
    assert decision.metadata["content_type"] == "mixed"
    assert decision.metadata["material_status"] == "needs_materials"
    assert decision.metadata["risk_level"] in {"fact_sensitive", "client_commitment"}


@pytest.mark.asyncio
async def test_content_sop_metadata_routes_planning_video_image_request() -> None:
    router = DCRouter(classifier=ExplodingClassifier())

    decision = await router.decide("策划部做一个短视频脚本和生图 prompt")

    assert decision.intent == RouterIntent.DEEP_CREATIVE.value
    assert decision.source == "content_sop"
    assert decision.metadata["content_sop"] == "true"
    assert decision.metadata["department"] == "planning"
    assert decision.metadata["content_type"] == "mixed"
    assert decision.metadata["material_status"] == "needs_materials"


@pytest.mark.asyncio
async def test_content_sop_copy_only_routes_creative_with_guard_metadata() -> None:
    router = DCRouter(classifier=ExplodingClassifier())

    decision = await router.decide("帮我写老客户复联话术，用在微信私域")

    assert decision.intent == RouterIntent.CREATIVE.value
    assert decision.source == "content_sop"
    assert decision.metadata["content_sop"] == "true"
    assert decision.metadata["department"] == "client_dept"
    assert decision.metadata["content_type"] == "copy"


@pytest.mark.asyncio
async def test_content_sop_metadata_marks_attachment_request_partial() -> None:
    router = DCRouter(classifier=ExplodingClassifier())

    decision = await router.decide(
        MessageEnvelope(
            text="帮我写客户邀约文案并配图",
            attachment_kinds=(AttachmentKind.FILE,),
            attachment_summary="附件里包含品牌、产品、目标受众和活动权益。",
        )
    )

    assert decision.metadata["content_sop"] == "true"
    assert decision.metadata["department"] == "client_dept"
    assert decision.metadata["content_type"] == "mixed"
    assert decision.metadata["material_status"] in {"partial", "ready"}
