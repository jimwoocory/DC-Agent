# ruff: noqa: E402
from __future__ import annotations

import sys
from pathlib import Path

# 必须早于 data/plugins/dc_router (那是 AstrBot plugin, 不含 classifier.py) —
# 之前 raw ``from dc_router.classifier import ...`` 会因为 tests/dc_router/
# test_config.py 等把 data/plugins 塞到 sys.path[0] 而撞上, 触发
# ``ModuleNotFoundError: No module named 'dc_router.classifier'``
# (regression 2026-06-11). 这里改用 canonical ``dc_router_core.*`` import,
# shim 包留作向后兼容, 不再在 test 里走它.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest

from dc_router_core.classifier import ClassifierResult
from dc_router_core.decision import RouterDecision
from dc_router_core.entrypoint import DCRouter, MessageEnvelope
from dc_router_core.ops_taxonomy import OpsIntent
from dc_router_core.provider_map import get_provider_route
from dc_router_core.rules import match_document_link, match_keywords, match_prefix
from dc_router_core.taxonomy import AttachmentKind, RouterIntent


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


def test_marketing_funnel_data_request_routes_to_deep_insight() -> None:
    match = match_keywords(
        "帮我整理一下上周抖音推广活动的投放数据和转化漏斗，"
        "对比小红书同期，找出抖音哪一步流失最大，给3条优化建议。"
    )

    assert match is not None
    assert match.intent is RouterIntent.DEEP_INSIGHT


@pytest.mark.asyncio
async def test_marketing_funnel_data_request_does_not_fallback() -> None:
    dc_router = DCRouter(classifier=ExplodingClassifier())

    decision = await dc_router.decide(
        MessageEnvelope(
            text=(
                "帮我整理一下上周抖音推广活动的投放数据和转化漏斗，"
                "对比小红书同期，找出抖音哪一步流失最大，给3条优化建议。"
            ),
            metadata={"platform_id": "巅池-Agent小助手"},
        )
    )

    assert decision.intent == RouterIntent.DEEP_INSIGHT.value
    assert decision.provider_id == "aihubmix/claude-opus-4-8"


def test_feishu_document_link_routes_to_multimodal_preprocess() -> None:
    match = match_document_link("看一下 https://demo.feishu.cn/docx/Abc_123")

    assert match is not None
    assert match.intent is RouterIntent.MULTIMODAL
    assert match.source == "document_link"


def test_router_intent_enum_is_explicit_and_complete() -> None:
    assert {intent.value for intent in RouterIntent} == {
        "casual",
        "work_preflight",
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
    dc_router = DCRouter(classifier=classifier)

    decision = await dc_router.decide(
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
    dc_router = DCRouter(classifier=FakeClassifier(None))

    decision = await dc_router.decide("这个事你怎么看")

    assert decision.intent == RouterIntent.FALLBACK.value
    assert decision.source == "fallback"
    assert decision.provider_id == "aihubmix/qwen3.7-max"


@pytest.mark.asyncio
async def test_business_router_preserves_feishu_channel_metadata() -> None:
    dc_router = DCRouter(classifier=ExplodingClassifier())

    decision = await dc_router.decide(
        MessageEnvelope(
            text="#创意 帮我写一版活动文案",
            metadata={
                "platform_id": "巅池-Agent小助手",
                "feishu_channel_agent_id": "planning-agent",
                "feishu_channel_workspace": "data/feishu_agents/planning-agent",
                "feishu_channel_peer_kind": "group",
                "feishu_channel_peer_id": "oc_group",
            },
        )
    )

    assert decision.intent == RouterIntent.CREATIVE.value
    assert decision.metadata["feishu_channel_agent_id"] == "planning-agent"
    assert decision.metadata["feishu_channel_workspace"] == (
        "data/feishu_agents/planning-agent"
    )
    assert decision.metadata["feishu_channel_peer_kind"] == "group"
    assert decision.metadata["feishu_channel_peer_id"] == "oc_group"


@pytest.mark.asyncio
async def test_realtime_routes_to_qwen_max() -> None:
    dc_router = DCRouter(classifier=ExplodingClassifier())

    decision = await dc_router.decide("今天行业有什么热点")

    assert decision.intent == RouterIntent.REALTIME.value
    assert decision.provider_id == "aihubmix/qwen3.7-max"


@pytest.mark.asyncio
async def test_attachment_without_summary_requires_multimodal_preprocess() -> None:
    dc_router = DCRouter(classifier=ExplodingClassifier())

    decision = await dc_router.decide(
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
    dc_router = DCRouter(classifier=ExplodingClassifier())

    decision = await dc_router.decide(
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
    dc_router = DCRouter(classifier=ExplodingClassifier())

    decision = await dc_router.decide(
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
    dc_router = DCRouter(classifier=ExplodingClassifier())

    decision = await dc_router.decide(
        MessageEnvelope(
            text="队列和凭证池现在怎么样",
            metadata={"platform_id": "巅池-技术"},
        )
    )

    assert decision.intent == OpsIntent.QUOTA_GATE_VIEW.value
    assert decision.source == "keyword"


@pytest.mark.asyncio
async def test_content_sop_metadata_routes_client_copy_image_request() -> None:
    dc_router = DCRouter(classifier=ExplodingClassifier())

    decision = await dc_router.decide("帮我写客户邀约文案并配图，用在私域活动触达")

    assert decision.intent == RouterIntent.DEEP_CREATIVE.value
    assert decision.source == "content_sop"
    assert decision.metadata["content_sop"] == "true"
    assert decision.metadata["department"] == "client_dept"
    assert decision.metadata["content_type"] == "mixed"
    assert decision.metadata["material_status"] == "needs_materials"
    assert decision.metadata["risk_level"] in {"fact_sensitive", "client_commitment"}


@pytest.mark.asyncio
async def test_content_sop_metadata_routes_planning_video_image_request() -> None:
    dc_router = DCRouter(classifier=ExplodingClassifier())

    decision = await dc_router.decide("策划部做一个短视频脚本和生图 prompt")

    assert decision.intent == RouterIntent.CREATIVE.value
    assert decision.source == "department_workflow"
    assert decision.metadata["content_sop"] == "true"
    assert decision.metadata["department"] == "planning"
    assert decision.metadata["department_workflow"] == "planning_creative_fast"
    assert decision.metadata["content_type"] == "mixed"
    assert decision.metadata["material_policy"] == "optional_for_first_draft"


@pytest.mark.asyncio
async def test_planning_framework_request_routes_to_fast_creative() -> None:
    dc_router = DCRouter(classifier=ExplodingClassifier())

    decision = await dc_router.decide("帮我梳理一个夏季活动方案框架，先给创意方向")

    assert decision.intent == RouterIntent.CREATIVE.value
    assert decision.provider_id == "aihubmix/doubao-seed-2-1-pro"
    assert decision.source == "department_workflow"
    assert decision.metadata["department"] == "planning"
    assert decision.metadata["department_workflow"] == "planning_creative_fast"
    assert decision.metadata["answer_policy"] == "answer_first"
    assert decision.metadata["memory_policy"] == "skip_deep_company_memory_by_default"
    assert decision.metadata["planning_output_mode"] == "framework_first"
    assert (
        decision.metadata["framework_policy"]
        == "toc_then_slide_titles_then_page_details"
    )


@pytest.mark.asyncio
async def test_middle_office_strategy_routes_to_planning_fast_creative() -> None:
    dc_router = DCRouter(classifier=ExplodingClassifier())

    decision = await dc_router.decide(
        "中台策略部帮我梳理一个活动方案框架，先给创意方向"
    )

    assert decision.intent == RouterIntent.CREATIVE.value
    assert decision.source == "department_workflow"
    assert decision.metadata["department"] == "planning"
    assert decision.metadata["department_workflow"] == "planning_creative_fast"


@pytest.mark.asyncio
async def test_middle_office_client_does_not_route_as_planning() -> None:
    dc_router = DCRouter(classifier=ExplodingClassifier())

    decision = await dc_router.decide("中台客户部帮我写客户活动邀约话术，用在微信私域")

    assert decision.intent == RouterIntent.DEEP_CREATIVE.value
    assert decision.source == "content_sop"
    assert decision.metadata["department"] == "client_dept"
    assert "department_workflow" not in decision.metadata


@pytest.mark.asyncio
async def test_execution_department_delivery_routes_to_work_preflight() -> None:
    dc_router = DCRouter(classifier=ExplodingClassifier())

    decision = await dc_router.decide("执行部门帮我整理场地物料安装点检和验收材料")

    assert decision.intent == RouterIntent.WORK_PREFLIGHT.value
    assert decision.source == "department_workflow"
    assert decision.metadata["department"] == "execution_ops"
    assert decision.metadata["department_workflow"] == "execution_delivery"
    assert decision.metadata["queue_policy"] == "no_queue_without_resource_reason"


@pytest.mark.asyncio
async def test_execution_gift_inventory_routes_to_work_preflight() -> None:
    dc_router = DCRouter(classifier=ExplodingClassifier())

    decision = await dc_router.decide("帮我做礼品打样生产跟踪和入库出库库存清单")

    assert decision.intent == RouterIntent.WORK_PREFLIGHT.value
    assert decision.source == "department_workflow"
    assert decision.metadata["department"] == "execution_ops"


@pytest.mark.asyncio
async def test_split_execution_departments_route_to_own_workflows() -> None:
    dc_router = DCRouter(classifier=ExplodingClassifier())

    design = await dc_router.decide("设计部帮我检查设计稿VI、字体和尺寸比例")
    film = await dc_router.decide("影视制作部帮我整理拍摄通告、机位安排和成片交付")
    ai_app = await dc_router.decide("AI应用部帮我设计部门小助手和知识库接入工作流")

    assert design.intent == RouterIntent.WORK_PREFLIGHT.value
    assert design.metadata["department"] == "design_dept"
    assert design.metadata["department_workflow"] == "design_delivery"
    assert film.intent == RouterIntent.WORK_PREFLIGHT.value
    assert film.metadata["department"] == "film_production"
    assert film.metadata["department_workflow"] == "film_production_delivery"
    assert ai_app.intent == RouterIntent.WORK_PREFLIGHT.value
    assert ai_app.metadata["department"] == "ai_application"
    assert ai_app.metadata["department_workflow"] == "ai_application_enablement"


@pytest.mark.asyncio
async def test_brand_publicity_ops_routes_to_work_preflight() -> None:
    dc_router = DCRouter(classifier=ExplodingClassifier())

    decision = await dc_router.decide("品宣部帮我整理媒介KOC任务下发和社群舆情复盘")

    assert decision.intent == RouterIntent.WORK_PREFLIGHT.value
    assert decision.source == "department_workflow"
    assert decision.metadata["department"] == "brand_publicity"
    assert decision.metadata["department_workflow"] == "brand_publicity_ops"
    assert decision.metadata["branch_policy"] == "liuqi_placeholder_only"
    assert decision.metadata["queue_policy"] == "no_queue_without_resource_reason"


@pytest.mark.asyncio
async def test_brand_publicity_does_not_steal_strategy_or_direct_media() -> None:
    dc_router = DCRouter(classifier=ExplodingClassifier())

    strategy = await dc_router.decide("中台账号运营要怎么设计栏目和直播节奏")
    direct_media = await dc_router.decide("品宣部帮我生成一张端午海报")

    assert strategy.metadata["department"] == "planning"
    assert strategy.metadata["department_workflow"] == "planning_creative_fast"
    assert direct_media.metadata["department_workflow"] == "planning_direct_media"


@pytest.mark.asyncio
async def test_brand_middle_platform_coordination_is_context_only() -> None:
    dc_router = DCRouter(classifier=FakeClassifier(None))

    decision = await dc_router.decide(
        MessageEnvelope(
            text="品宣和中台策略部都提到直播账号运营，这个先怎么分工？",
            metadata={"platform_id": "巅池-Agent小助手"},
        )
    )

    assert decision.source != "department_workflow"
    assert "department_workflow" not in decision.metadata
    assert decision.metadata["rdf_department_signal"] == "department_context"
    assert "department_context_not_entrypoint" in decision.metadata["rule_gate_notes"]


@pytest.mark.asyncio
async def test_liuq_brand_context_keeps_org_interface_context_only() -> None:
    dc_router = DCRouter(classifier=FakeClassifier(None))

    decision = await dc_router.decide(
        MessageEnvelope(
            text="柳汽这边的品宣账号运营怎么先保留组织接口？",
            metadata={"platform_id": "巅池-Agent小助手"},
        )
    )

    assert decision.source != "department_workflow"
    assert "department_workflow" not in decision.metadata
    assert decision.metadata["rdf_department_signal"] == "department_context"
    assert "department_context_not_entrypoint" in decision.metadata["rule_gate_notes"]


@pytest.mark.asyncio
async def test_liuq_branch_name_alone_does_not_create_active_brand_route() -> None:
    dc_router = DCRouter(classifier=FakeClassifier(None))

    decision = await dc_router.decide("柳汽这边今天有什么安排")

    assert decision.metadata.get("department") != "brand_publicity"
    assert decision.metadata.get("department_workflow") != "brand_publicity_ops"


@pytest.mark.asyncio
async def test_execution_leaf_department_does_not_steal_direct_media_request() -> None:
    dc_router = DCRouter(classifier=ExplodingClassifier())

    decision = await dc_router.decide(
        MessageEnvelope(
            text="帮我生成一张缤果 Pro 夏至海报",
            metadata={"requester_department": "设计部"},
        )
    )

    assert decision.metadata.get("department_workflow") != "execution_delivery"
    assert decision.metadata.get("media_policy") == "direct_generation"


@pytest.mark.asyncio
async def test_planning_direct_media_request_routes_without_prompt_coaching() -> None:
    dc_router = DCRouter(classifier=ExplodingClassifier())

    decision = await dc_router.decide("帮我生成一张缤果 Pro 夏至海报")

    assert decision.intent == RouterIntent.CREATIVE.value
    assert decision.source == "department_workflow"
    assert decision.metadata["department_workflow"] == "planning_direct_media"
    assert decision.metadata["answer_policy"] == "generate_directly_no_prompt_coaching"
    assert decision.metadata["default_aspect_ratio"] == "portrait"


@pytest.mark.asyncio
async def test_planning_research_request_requires_sources() -> None:
    dc_router = DCRouter(classifier=ExplodingClassifier())

    decision = await dc_router.decide("帮我找一下小红书和抖音最近竞品活动趋势")

    assert decision.intent == RouterIntent.REALTIME.value
    assert decision.source == "department_workflow"
    assert decision.metadata["department"] == "planning"
    assert decision.metadata["department_workflow"] == "planning_research_with_sources"
    assert decision.metadata["search_required"] == "true"
    assert decision.metadata["source_policy"] == "separate_facts_from_assumptions"


@pytest.mark.asyncio
async def test_content_sop_copy_only_routes_creative_with_guard_metadata() -> None:
    dc_router = DCRouter(classifier=ExplodingClassifier())

    decision = await dc_router.decide("帮我写老客户复联话术，用在微信私域")

    assert decision.intent == RouterIntent.CREATIVE.value
    assert decision.source == "content_sop"
    assert decision.metadata["content_sop"] == "true"
    assert decision.metadata["department"] == "client_dept"
    assert decision.metadata["content_type"] == "copy"


@pytest.mark.asyncio
async def test_content_sop_metadata_marks_attachment_request_partial() -> None:
    dc_router = DCRouter(classifier=ExplodingClassifier())

    decision = await dc_router.decide(
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


# ───────────────── Classifier timeout & error resilience ─────────────────


class HangingClassifier:
    """Simulates a classifier that hangs indefinitely."""

    async def classify(self, text: str) -> ClassifierResult | None:
        import asyncio

        await asyncio.sleep(999)
        return None  # pragma: no cover


class BrokenClassifier:
    """Simulates a classifier that raises an unexpected exception."""

    async def classify(self, text: str) -> ClassifierResult | None:
        msg = "simulated provider failure"
        raise RuntimeError(msg)


@pytest.mark.asyncio
async def test_classifier_timeout_falls_back_to_rules() -> None:
    """When classifier hangs, _maybe_classify should timeout and return FALLBACK."""
    import dc_router_core.entrypoint as ep

    original = ep.CLASSIFIER_TIMEOUT_SECONDS
    ep.CLASSIFIER_TIMEOUT_SECONDS = 0.1  # fast timeout for test
    try:
        dc_router = DCRouter(classifier=HangingClassifier())
        decision = await dc_router.decide("这句没有任何关键词匹配的话")

        # Should fall through to FALLBACK (not hang forever)
        assert decision.intent == RouterIntent.FALLBACK.value
        assert decision.source == "fallback"
    finally:
        ep.CLASSIFIER_TIMEOUT_SECONDS = original


@pytest.mark.asyncio
async def test_classifier_exception_falls_back_to_rules() -> None:
    """When classifier raises, _maybe_classify should catch and return FALLBACK."""
    dc_router = DCRouter(classifier=BrokenClassifier())
    decision = await dc_router.decide("这句没有任何关键词匹配的话")

    assert decision.intent == RouterIntent.FALLBACK.value
    assert decision.source == "fallback"
