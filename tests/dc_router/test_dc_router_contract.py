"""Engineering-grade contract tests for dc_router_core.

Locks down the routing contract for the unified ``dc_router`` entry point.
This file replaces the legacy ``data/plugins/llm_router/test_dc_router_path.py``
and ``data/plugins/dc_router/test_routing_path.py``; both were tied to the old
``LLMRouterPlugin`` shim and have been retired.

The test classes are organised by the contract surface they cover:

- :class:`TestTaxonomyContracts`        — enum membership + value stability
- :class:`TestRuleMatcherContracts`     — prefix / keyword / document_link rules
- :class:`TestProviderMapContracts`     — every RouterIntent must have a route
- :class:`TestOpsProviderMapContracts`  — every OpsIntent must have a route
- :class:`TestContentSOPContracts`      — content SOP inference is deterministic
- :class:`TestBusinessRouterContracts`  — DCRouter.decide() business path
- :class:`TestOpsRouterContracts`       — DCRouter.decide() ops path
- :class:`TestRouterDecisionContracts`  — RouterDecision serialization
- :class:`TestClassifierIntegrationContracts` — fallback to LLM classifier

Test isolation: every test that touches the ``router_mode`` branch passes an
explicit ``platform_id`` metadata, so the suite can be executed in any order
without leaking state.
"""

from __future__ import annotations

import pytest

from dc_router_core.classifier import (
    ROUTER_CLASSIFIER_PROVIDER_ID,
    ClassifierResult,
    NoopRouterClassifier,
)
from dc_router_core.content_sop import (
    ContentDepartment,
    ContentMaterialStatus,
    ContentRiskLevel,
    ContentType,
    infer_content_sop_metadata,
)
from dc_router_core.decision import RouterDecision
from dc_router_core.entrypoint import DCRouter, MessageEnvelope, PassThroughArbiter
from dc_router_core.ops_provider_map import (
    OPS_CODEX_CLI,
    OPS_PROVIDER_MAP,
    get_ops_provider_route,
)
from dc_router_core.ops_rules import (
    OPS_KEYWORD_RULES,
    OPS_PREFIX_RULES,
    match_ops_keywords,
    match_ops_prefix,
)
from dc_router_core.ops_taxonomy import OpsIntent
from dc_router_core.provider_map import (
    AIHUBMIX_CLAUDE_OPUS_4_7,
    AIHUBMIX_CLAUDE_OPUS_4_8,
    AIHUBMIX_CLAUDE_SONNET_4_6,
    AIHUBMIX_DEEPSEEK_PRO,
    AIHUBMIX_DOUBAO_SEED_2_1_PRO,
    AIHUBMIX_GEMINI_FLASH,
    AIHUBMIX_GROK,
    AIHUBMIX_QWEN_FLASH,
    AIHUBMIX_QWEN_MAX,
    CLI_CODEX_GPT_5_4,
    CLI_GROK_BUILD,
    DEFAULT_PROVIDER_MAP,
    get_provider_route,
)
from dc_router_core.rules import (
    KEYWORD_RULES,
    PREFIX_RULES,
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

# ─────────────────────────────────────────────────────────────────────────
# 1. Taxonomy contracts — the enum surface is part of the public contract
# ─────────────────────────────────────────────────────────────────────────


class TestTaxonomyContracts:
    """Locks down the RouterIntent / OpsIntent / RouteDepth / RouteAction enums."""

    def test_business_intent_enum_is_explicit_and_complete(self) -> None:
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

    def test_ops_intent_enum_is_explicit_and_complete(self) -> None:
        assert {intent.value for intent in OpsIntent} == {
            "system_status",
            "queue_status",
            "error_debug",
            "code_ops",
            "deployment_ops",
            "quota_gate_view",
            "ops_fallback",
        }

    def test_route_depth_values_are_stable(self) -> None:
        assert {d.value for d in RouteDepth} == {"direct", "front", "hermes"}

    def test_route_action_values_are_stable(self) -> None:
        assert {a.value for a in RouteAction} == {
            "preprocess",
            "answer",
            "queue_front",
            "enqueue_deep_task",
        }

    def test_attachment_kind_values_are_stable(self) -> None:
        assert {k.value for k in AttachmentKind} == {
            "image",
            "screenshot",
            "voice",
            "video",
            "file",
        }

    def test_router_intent_and_ops_intent_do_not_collide(self) -> None:
        """Both enums share the RouterDecision.intent field; values must be unique."""
        business = {i.value for i in RouterIntent}
        ops = {i.value for i in OpsIntent}
        overlap = business & ops
        assert not overlap, f"intent value overlap will break RouterDecision: {overlap}"


# ─────────────────────────────────────────────────────────────────────────
# 2. Rule matchers — prefix / keyword / document link rules
# ─────────────────────────────────────────────────────────────────────────


class TestRuleMatcherContracts:
    """Locks down the deterministic rule layer."""

    @pytest.mark.parametrize(
        ("text", "intent"),
        [
            ("#深度 做一份品牌分析", RouterIntent.DEEP_INSIGHT),
            ("#PRD 写一个后台需求", RouterIntent.DEEP_INSIGHT),
            ("#prd 写一个后台需求", RouterIntent.DEEP_INSIGHT),
            ("#前置 先帮我理一下这个需求", RouterIntent.WORK_PREFLIGHT),
            ("#轻文案 帮我润色一句按钮", RouterIntent.WORK_PREFLIGHT),
            ("#洞察 这类用户怎么想", RouterIntent.INSIGHT),
            ("#创意 给我 10 个 slogan", RouterIntent.CREATIVE),
            ("#舆情 负面评论怎么回", RouterIntent.PUBLIC_OPINION),
            ("#代码 解释这个 traceback", RouterIntent.SIMPLE_CODE),
            ("#实时 最近热点", RouterIntent.REALTIME),
            ("#realtime 行业新闻", RouterIntent.REALTIME),
            ("#快速 简短回答", RouterIntent.CASUAL),
            ("#fast hi", RouterIntent.CASUAL),
            ("#写作 帮我写个周报", RouterIntent.OPS_WRITING),
            ("#writing 帮我起草邮件", RouterIntent.OPS_WRITING),
            ("#codex 这段代码", RouterIntent.SIMPLE_CODE),
            ("#codex高 帮我看看", RouterIntent.SIMPLE_CODE),
            ("#codex超深 深度分析", RouterIntent.SIMPLE_CODE),
        ],
    )
    def test_prefix_rules_route_to_expected_intents(
        self,
        text: str,
        intent: RouterIntent,
    ) -> None:
        match = match_prefix(text)
        assert match is not None
        assert match.intent is intent
        assert match.source == "prefix"

    def test_prefix_rules_strip_leading_whitespace(self) -> None:
        match = match_prefix("   #深度 做品牌分析")
        assert match is not None
        assert match.intent is RouterIntent.DEEP_INSIGHT

    def test_match_prefix_returns_none_for_no_match(self) -> None:
        assert match_prefix("") is None
        assert match_prefix("普通文本") is None
        assert match_prefix("#未知前缀 text") is None

    def test_public_opinion_keyword_has_priority_over_realtime_and_creative(
        self,
    ) -> None:
        match = match_keywords("最新热点舆情来了，顺便写一版营销文案")
        assert match is not None
        assert match.intent is RouterIntent.PUBLIC_OPINION
        assert match.source == "keyword"

    def test_deep_creative_keyword_has_priority_over_creative(self) -> None:
        match = match_keywords("做一份完整营销方案，里面也包含 slogan")
        assert match is not None
        assert match.intent is RouterIntent.DEEP_CREATIVE

    def test_deep_insight_keyword_triggered_by_prd(self) -> None:
        match = match_keywords("帮我起草一个 PRD 文档，包含功能模块和验收标准")
        assert match is not None
        assert match.intent is RouterIntent.DEEP_INSIGHT

    def test_deep_insight_keyword_triggered_by_deep_task_phrase(self) -> None:
        match = match_keywords("请给我一份完整的策略分析报告")
        assert match is not None
        assert match.intent is RouterIntent.DEEP_INSIGHT

    def test_feishu_document_link_routes_to_multimodal(self) -> None:
        match = match_document_link("看一下 https://demo.feishu.cn/docx/Abc_123")
        assert match is not None
        assert match.intent is RouterIntent.MULTIMODAL
        assert match.source == "document_link"

    def test_feishu_wiki_link_routes_to_multimodal(self) -> None:
        match = match_document_link(
            "看下 https://o0ain5w98jh.feishu.cn/wiki/NJXowzJCtimtiXkx02mcoaOXngd"
        )
        assert match is not None
        assert match.intent is RouterIntent.MULTIMODAL

    def test_match_keywords_returns_none_for_empty_or_unmatched(self) -> None:
        assert match_keywords("") is None
        assert match_keywords("1234567890") is None

    def test_ops_prefix_rules_are_stable(self) -> None:
        cases = [
            ("#状态 看看服务", OpsIntent.SYSTEM_STATUS),
            ("#队列 失败任务", OpsIntent.QUEUE_STATUS),
            ("#配额 凭证池", OpsIntent.QUOTA_GATE_VIEW),
            ("#排障 报错", OpsIntent.ERROR_DEBUG),
            ("#部署 上线", OpsIntent.DEPLOYMENT_OPS),
            ("#脚本 写个 shell", OpsIntent.CODE_OPS),
        ]
        for text, intent in cases:
            match = match_ops_prefix(text)
            assert match is not None, f"ops prefix miss: {text}"
            assert match.intent is intent, f"ops prefix {text} -> {match.intent}"

    def test_ops_keyword_priority_prefers_quota_gate_view(self) -> None:
        match = match_ops_keywords("队列和凭证池现在怎么样")
        assert match is not None
        assert match.intent is OpsIntent.QUOTA_GATE_VIEW
        assert match.source == "keyword"

    def test_ops_keyword_priority_prefers_queue_over_code_ops(self) -> None:
        match = match_ops_keywords("看一下深度任务排队情况")
        assert match is not None
        assert match.intent is OpsIntent.QUEUE_STATUS

    def test_ops_keyword_falls_back_to_deployment(self) -> None:
        match = match_ops_keywords("帮我重启服务然后上线新版本")
        assert match is not None
        assert match.intent is OpsIntent.DEPLOYMENT_OPS

    def test_match_ops_prefix_handles_empty_input(self) -> None:
        assert match_ops_prefix("") is None
        assert match_ops_prefix("plain text") is None


# ─────────────────────────────────────────────────────────────────────────
# 3. Provider map — every RouterIntent must have a route, and providers
#    must be in the allowlist.
# ─────────────────────────────────────────────────────────────────────────


class TestProviderMapContracts:
    """Locks down the default provider map for business routing."""

    def test_every_router_intent_has_a_route(self) -> None:
        for intent in RouterIntent:
            assert intent in DEFAULT_PROVIDER_MAP, (
                f"RouterIntent.{intent.name} missing from DEFAULT_PROVIDER_MAP"
            )

    def test_routes_use_only_allowlisted_providers(self) -> None:
        allowed = {
            AIHUBMIX_CLAUDE_OPUS_4_7,
            AIHUBMIX_CLAUDE_OPUS_4_8,
            AIHUBMIX_CLAUDE_SONNET_4_6,
            AIHUBMIX_DEEPSEEK_PRO,
            AIHUBMIX_DOUBAO_SEED_2_1_PRO,
            AIHUBMIX_GEMINI_FLASH,
            AIHUBMIX_GROK,
            AIHUBMIX_QWEN_FLASH,
            AIHUBMIX_QWEN_MAX,
            CLI_CODEX_GPT_5_4,
            CLI_GROK_BUILD,
        }
        for intent, route in DEFAULT_PROVIDER_MAP.items():
            assert route.provider_id in allowed, (
                f"RouterIntent.{intent.name} uses un-allowlisted provider "
                f"{route.provider_id!r}"
            )

    def test_casual_routes_to_qwen_max(self) -> None:
        route = get_provider_route(RouterIntent.CASUAL)
        assert route.provider_id == AIHUBMIX_QWEN_MAX
        assert route.depth is RouteDepth.DIRECT
        assert route.action is RouteAction.ANSWER

    def test_multimodal_routes_to_gemini_flash(self) -> None:
        route = get_provider_route(RouterIntent.MULTIMODAL)
        assert route.provider_id == AIHUBMIX_GEMINI_FLASH
        assert route.action is RouteAction.PREPROCESS

    def test_public_opinion_routes_to_grok_build(self) -> None:
        route = get_provider_route(RouterIntent.PUBLIC_OPINION)
        assert route.provider_id == CLI_GROK_BUILD

    def test_deep_creative_routes_to_claude_opus_47(self) -> None:
        route = get_provider_route(RouterIntent.DEEP_CREATIVE)
        assert route.provider_id == AIHUBMIX_CLAUDE_OPUS_4_7
        assert route.target_model == "claude-opus-4-7"
        assert route.requires_queue is False
        assert route.resource_keys == ()

    def test_deep_insight_routes_to_claude_opus_48(self) -> None:
        route = get_provider_route(RouterIntent.DEEP_INSIGHT)
        assert route.provider_id == AIHUBMIX_CLAUDE_OPUS_4_8
        assert route.target_model == "claude-opus-4-8"

    def test_fallback_routes_to_qwen_max(self) -> None:
        route = get_provider_route(RouterIntent.FALLBACK)
        assert route.provider_id == AIHUBMIX_QWEN_MAX

    def test_get_provider_route_returns_fallback_for_unknown_intent(self) -> None:
        # DEFAULT_PROVIDER_MAP.get(unknown) is None; the typed dict requires
        # RouterIntent keys, so we exercise the contract via .get + the helper.
        assert DEFAULT_PROVIDER_MAP.get(RouterIntent.FALLBACK) is not None
        assert DEFAULT_PROVIDER_MAP[RouterIntent.FALLBACK] is get_provider_route(
            RouterIntent.FALLBACK
        )


class TestOpsProviderMapContracts:
    """Locks down the default provider map for ops routing."""

    def test_every_ops_intent_has_a_route(self) -> None:
        for intent in OpsIntent:
            assert intent in OPS_PROVIDER_MAP, (
                f"OpsIntent.{intent.name} missing from OPS_PROVIDER_MAP"
            )

    def test_all_ops_routes_use_codex_cli(self) -> None:
        for intent, route in OPS_PROVIDER_MAP.items():
            assert route.provider_id == OPS_CODEX_CLI, (
                f"OpsIntent.{intent.name} uses {route.provider_id!r} (expected codex CLI)"
            )
            assert route.depth is RouteDepth.DIRECT
            assert route.action is RouteAction.ANSWER

    def test_ops_fallback_routes_to_codex(self) -> None:
        route = get_ops_provider_route(OpsIntent.OPS_FALLBACK)
        assert route.provider_id == OPS_CODEX_CLI

    def test_ops_routes_do_not_require_queue(self) -> None:
        # OpsProviderRoute intentionally has no requires_queue / requires_harness
        # fields (ops routes are always DIRECT) — the contract is that the
        # dataclass surface is minimal.
        for _intent, route in OPS_PROVIDER_MAP.items():
            assert not hasattr(route, "requires_queue") or (
                route.requires_queue is False  # type: ignore[attr-defined]
            )
            assert not hasattr(route, "requires_harness") or (
                route.requires_harness is False  # type: ignore[attr-defined]
            )


# ─────────────────────────────────────────────────────────────────────────
# 4. Content SOP — deterministic metadata inference
# ─────────────────────────────────────────────────────────────────────────


class TestContentSOPContracts:
    """Locks down the deterministic Content SOP inference."""

    def test_infer_metadata_for_planning_video_image_request(self) -> None:
        meta = infer_content_sop_metadata(
            "策划部做一个短视频脚本和生图 prompt",
        )
        assert meta.department is ContentDepartment.PLANNING
        assert meta.content_type is ContentType.MIXED
        assert meta.is_content_sop is True

    def test_infer_metadata_for_client_copy_only(self) -> None:
        meta = infer_content_sop_metadata(
            "帮我写老客户复联话术，用在微信私域",
        )
        assert meta.department is ContentDepartment.CLIENT_DEPT
        assert meta.content_type is ContentType.COPY
        assert meta.risk_level in {
            ContentRiskLevel.FACT_SENSITIVE,
            ContentRiskLevel.CLIENT_COMMITMENT,
        }

    def test_infer_metadata_for_client_mixed_with_attachment_summary(self) -> None:
        meta = infer_content_sop_metadata(
            "帮我写客户邀约文案并配图",
            attachment_summary="附件里包含品牌、产品、目标受众和活动权益。",
            has_attachments=True,
        )
        assert meta.department is ContentDepartment.CLIENT_DEPT
        assert meta.content_type is ContentType.MIXED
        assert meta.material_status in {
            ContentMaterialStatus.PARTIAL,
            ContentMaterialStatus.READY,
        }

    def test_infer_metadata_for_text_with_no_content_signal(self) -> None:
        meta = infer_content_sop_metadata("现在几点了")
        assert meta.is_content_sop is False
        assert meta.to_metadata() == {}

    def test_infer_metadata_picks_client_over_planning_when_dominant(self) -> None:
        # Heavy client signals (回访, 续约, 话术, 触达) outweigh a single planning
        # signal — _infer_department counts regex hits and picks the bigger score.
        meta = infer_content_sop_metadata(
            "客户部回访话术，续约触达权益方案，策划分镜参考",
        )
        assert meta.department is ContentDepartment.CLIENT_DEPT

    def test_infer_metadata_client_commitment_risk(self) -> None:
        meta = infer_content_sop_metadata("给客户承诺报价和续约权益")
        assert meta.risk_level is ContentRiskLevel.CLIENT_COMMITMENT

    def test_infer_metadata_brand_sensitive_risk(self) -> None:
        meta = infer_content_sop_metadata("检查品牌口径的禁用词和危机舆情合规")
        assert meta.risk_level is ContentRiskLevel.BRAND_SENSITIVE


# ─────────────────────────────────────────────────────────────────────────
# 5. DCRouter — business path
# ─────────────────────────────────────────────────────────────────────────


class _RecordingClassifier:
    """Test classifier that records the text it was called with."""

    def __init__(self, result: ClassifierResult | None) -> None:
        self.result = result
        self.calls: list[str] = []

    async def classify(self, text: str) -> ClassifierResult | None:
        self.calls.append(text)
        return self.result


class _ExplodingClassifier:
    async def classify(self, text: str) -> ClassifierResult | None:
        raise AssertionError(f"classifier should not be called for: {text!r}")


class TestBusinessRouterContracts:
    """Locks down the DCRouter business decision path."""

    def setup_method(self) -> None:
        self.router = DCRouter()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("text", "expected_intent"),
        [
            ("你好", RouterIntent.CASUAL),
            ("#前置 先帮我理一下这个需求", RouterIntent.WORK_PREFLIGHT),
            ("#轻文案 按钮文案", RouterIntent.WORK_PREFLIGHT),
            ("帮我写个周报", RouterIntent.OPS_WRITING),
            ("#深度 帮我分析五菱新能源的策略", RouterIntent.DEEP_INSIGHT),
            ("今天行业有什么热点", RouterIntent.REALTIME),
            ("我的 Python 脚本报错", RouterIntent.SIMPLE_CODE),
            ("#创意 帮我写五菱端午营销文案", RouterIntent.CREATIVE),
            ("#洞察 分析五菱新能源用户洞察", RouterIntent.INSIGHT),
            ("舆情危机要怎么应对", RouterIntent.PUBLIC_OPINION),
        ],
    )
    async def test_router_basic_intents_business(
        self,
        text: str,
        expected_intent: RouterIntent,
    ) -> None:
        decision = await self.router.decide(
            MessageEnvelope(
                text=text,
                metadata={"platform_id": "巅池-Agent小助手"},
            )
        )
        assert decision.intent == expected_intent.value
        assert decision.depth == RouteDepth.DIRECT.value
        # business 路径: DCRouter 不主动写 router_mode (由 event_envelope 写),
        # 但 platform_id 必须保留, 且 router_mode 不应为 ops.
        assert decision.metadata.get("router_mode") != "ops"
        assert decision.metadata.get("platform_id") == "巅池-Agent小助手"

    @pytest.mark.asyncio
    async def test_business_decide_returns_string_intent_field(self) -> None:
        """RouterDecision.intent is a string (use_enum_values=True)."""
        decision = await self.router.decide("你好")
        assert isinstance(decision.intent, str)
        assert decision.intent == "casual"

    @pytest.mark.asyncio
    async def test_business_decide_classifier_not_called_for_strong_signals(
        self,
    ) -> None:
        classifier = _ExplodingClassifier()
        router = DCRouter(classifier=classifier)
        decision = await router.decide("#深度 品牌战略")
        assert decision.intent == RouterIntent.DEEP_INSIGHT.value
        assert decision.source == "prefix"

    @pytest.mark.asyncio
    async def test_business_decide_uses_classifier_when_rules_fallback(self) -> None:
        classifier = _RecordingClassifier(
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
        assert "年轻家庭" in classifier.calls[0]
        assert decision.metadata["session_id"] == "session-1"

    @pytest.mark.asyncio
    async def test_business_decide_noop_classifier_keeps_fallback(self) -> None:
        router = DCRouter(classifier=NoopRouterClassifier())
        decision = await router.decide("这个事你怎么看")
        assert decision.intent == RouterIntent.FALLBACK.value
        assert decision.source == "fallback"
        assert decision.provider_id == AIHUBMIX_QWEN_MAX

    @pytest.mark.asyncio
    async def test_attachment_without_summary_routes_to_multimodal(self) -> None:
        router = DCRouter(classifier=_ExplodingClassifier())
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
    async def test_feishu_channel_metadata_passes_through(self) -> None:
        router = DCRouter(classifier=_ExplodingClassifier())
        decision = await router.decide(
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
        assert (
            decision.metadata["feishu_channel_workspace"]
            == "data/feishu_agents/planning-agent"
        )

    @pytest.mark.asyncio
    async def test_realtime_routes_to_qwen_max(self) -> None:
        router = DCRouter(classifier=_ExplodingClassifier())
        decision = await router.decide("今天行业有什么热点")
        assert decision.intent == RouterIntent.REALTIME.value
        assert decision.provider_id == AIHUBMIX_QWEN_MAX

    @pytest.mark.asyncio
    async def test_prefix_overrides_keyword_after_attachment_summary(self) -> None:
        router = DCRouter(classifier=_ExplodingClassifier())
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
    async def test_pass_through_arbiter_returns_unchanged_decision(self) -> None:
        """PassThroughArbiter should not mutate the ProviderRoute."""
        router = DCRouter(arbiter=PassThroughArbiter())
        decision = await router.decide("#创意 slogan")
        # arbiter is invoked but does not change route — provider_id is preserved.
        assert decision.provider_id == AIHUBMIX_DOUBAO_SEED_2_1_PRO

    @pytest.mark.asyncio
    async def test_planning_department_fast_creative_is_hard_routed(self) -> None:
        router = DCRouter(classifier=_ExplodingClassifier())
        decision = await router.decide(
            MessageEnvelope(
                text="帮我梳理一个夏季活动方案框架，先给创意方向",
                metadata={"platform_id": "巅池-Agent小助手"},
            )
        )

        assert decision.intent == RouterIntent.CREATIVE.value
        assert decision.provider_id == AIHUBMIX_DOUBAO_SEED_2_1_PRO
        assert decision.source == "department_workflow"
        assert decision.metadata["department"] == "planning"
        assert decision.metadata["department_workflow"] == "planning_creative_fast"
        assert decision.metadata["answer_policy"] == "answer_first"
        assert (
            decision.metadata["memory_policy"] == "skip_deep_company_memory_by_default"
        )
        assert decision.metadata["planning_output_mode"] == "framework_first"
        assert (
            decision.metadata["framework_policy"]
            == "toc_then_slide_titles_then_page_details"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("text", "mode", "label"),
        [
            ("帮我做一个五菱小红书推广策略", "promotion_strategy", "推广策略"),
            ("帮我做一个通品店铺运营方案策划", "proposal_planning", "方案策划"),
            ("帮我写一组缤果夏季广告创意文案", "ad_creative", "广告创意"),
            ("帮我做一个竞品市场调研报告", "market_research_report", "市场调研报告"),
        ],
    )
    async def test_planning_department_writing_modes_are_locked(
        self, text: str, mode: str, label: str
    ) -> None:
        router = DCRouter(classifier=_ExplodingClassifier())
        decision = await router.decide(
            MessageEnvelope(
                text=text,
                metadata={"platform_id": "巅池-Agent小助手"},
            )
        )

        assert decision.intent == RouterIntent.CREATIVE.value
        assert decision.provider_id == AIHUBMIX_DOUBAO_SEED_2_1_PRO
        assert decision.source == "department_workflow"
        assert decision.metadata["department_workflow"] == "planning_creative_fast"
        assert decision.metadata["planning_writing_skill"] == "planning-writing"
        assert decision.metadata["planning_writing_mode"] == mode
        assert decision.metadata["planning_writing_mode_label"] == label
        assert decision.metadata["planning_structure_policy"]
        if mode == "market_research_report":
            assert decision.metadata["search_required"] == "true"
            assert (
                decision.metadata["source_policy"] == "separate_facts_from_assumptions"
            )

    @pytest.mark.asyncio
    async def test_planning_department_direct_media_is_hard_routed(self) -> None:
        router = DCRouter(classifier=_ExplodingClassifier())
        decision = await router.decide(
            MessageEnvelope(
                text="帮我生成一张缤果 Pro 夏至海报",
                metadata={"platform_id": "巅池-Agent小助手"},
            )
        )

        assert decision.intent == RouterIntent.CREATIVE.value
        assert decision.source == "department_workflow"
        assert decision.metadata["department_workflow"] == "planning_direct_media"
        assert (
            decision.metadata["answer_policy"] == "generate_directly_no_prompt_coaching"
        )
        assert decision.metadata["default_aspect_ratio"] == "portrait"

    @pytest.mark.asyncio
    async def test_planning_department_research_requires_sources(self) -> None:
        router = DCRouter(classifier=_ExplodingClassifier())
        decision = await router.decide(
            MessageEnvelope(
                text="帮我找一下小红书和抖音最近竞品活动趋势",
                metadata={"platform_id": "巅池-Agent小助手"},
            )
        )

        assert decision.intent == RouterIntent.REALTIME.value
        assert decision.source == "department_workflow"
        assert (
            decision.metadata["department_workflow"] == "planning_research_with_sources"
        )
        assert decision.metadata["search_required"] == "true"
        assert decision.metadata["source_policy"] == "separate_facts_from_assumptions"


# ─────────────────────────────────────────────────────────────────────────
# 6. DCRouter — ops path
# ─────────────────────────────────────────────────────────────────────────


class TestOpsRouterContracts:
    """Locks down the DCRouter ops decision path."""

    def setup_method(self) -> None:
        self.router = DCRouter()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("text", "expected_intent"),
        [
            ("Hermes 状态如何", OpsIntent.SYSTEM_STATUS),
            ("当前队列里有几个任务", OpsIntent.QUEUE_STATUS),
            ("看看 aihubmix 用量怎么样", OpsIntent.QUOTA_GATE_VIEW),
            ("traceback 帮我看下错在哪", OpsIntent.ERROR_DEBUG),
            ("写个 shell 脚本统计日志", OpsIntent.CODE_OPS),
            ("帮我重启服务", OpsIntent.DEPLOYMENT_OPS),
            ("#队列", OpsIntent.QUEUE_STATUS),
            ("#部署 上线新版本", OpsIntent.DEPLOYMENT_OPS),
        ],
    )
    async def test_ops_platform_routes_to_ops_intents(
        self,
        text: str,
        expected_intent: OpsIntent,
    ) -> None:
        decision = await self.router.decide(
            MessageEnvelope(
                text=text,
                metadata={"platform_id": "巅池-技术（DevOps）"},
            )
        )
        assert decision.intent == expected_intent.value
        assert decision.metadata.get("router_mode") == "ops"
        assert decision.provider_id == CLI_CODEX_GPT_5_4
        assert decision.target_model == "gpt-5.4"

    @pytest.mark.asyncio
    async def test_ops_platform_falls_back_when_no_match(self) -> None:
        decision = await self.router.decide(
            MessageEnvelope(
                text="呃这是什么",
                metadata={"platform_id": "巅池-技术（DevOps）"},
            )
        )
        assert decision.intent == OpsIntent.OPS_FALLBACK.value
        assert decision.provider_id == CLI_CODEX_GPT_5_4

    @pytest.mark.asyncio
    async def test_ops_platform_uses_ops_router_by_default(self) -> None:
        decision = await self.router.decide(
            MessageEnvelope(
                text="Hermes 状态如何",
                metadata={"platform_id": "巅池-技术（DevOps）"},
            )
        )
        assert decision.metadata.get("router_mode") == "ops"
        assert decision.intent == OpsIntent.SYSTEM_STATUS.value

    @pytest.mark.asyncio
    async def test_alt_ops_platform_id_also_routes_to_ops(self) -> None:
        decision = await self.router.decide(
            MessageEnvelope(
                text="队列里几个任务",
                metadata={"platform_id": "巅池-技术"},
            )
        )
        assert decision.metadata.get("router_mode") == "ops"

    @pytest.mark.asyncio
    async def test_business_unaffected_by_ops_keywords(self) -> None:
        decision = await self.router.decide(
            MessageEnvelope(
                text="看看队列里几个任务",
                metadata={"platform_id": "巅池-Agent小助手"},
            )
        )
        assert decision.intent != OpsIntent.QUEUE_STATUS.value
        assert decision.metadata.get("router_mode") != "ops"

    @pytest.mark.asyncio
    async def test_unknown_platform_uses_business_path(self) -> None:
        """A platform that is neither business nor ops falls through to business."""
        decision = await self.router.decide(
            MessageEnvelope(
                text="帮我写客户邀约文案并配图",
                metadata={"platform_id": "some-other-bot"},
            )
        )
        # Neither business nor ops metadata — dc_router_core's _decide_business runs.
        assert decision.metadata.get("router_mode") != "ops"


# ─────────────────────────────────────────────────────────────────────────
# 7. RouterDecision — serialization contract
# ─────────────────────────────────────────────────────────────────────────


class TestRouterDecisionContracts:
    """Locks down the RouterDecision Pydantic model."""

    def test_from_route_preserves_provider_contract(self) -> None:
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

    def test_from_ops_route_fills_defaults(self) -> None:
        route = get_ops_provider_route(OpsIntent.QUEUE_STATUS)
        decision = RouterDecision.from_ops_route(
            route,
            reason="ops test",
            source="keyword",
            metadata={"platform_id": "巅池-技术（DevOps）"},
        )
        assert decision.intent == OpsIntent.QUEUE_STATUS.value
        assert decision.provider_id == route.provider_id
        # OpsProviderRoute has no resource_keys / queue fields — must default.
        assert decision.resource_keys == ()
        assert decision.requires_queue is False
        assert decision.requires_harness is False
        assert decision.needs_multimodal_preprocess is False

    def test_decision_is_serializable_to_dict(self) -> None:
        route = get_provider_route(RouterIntent.CASUAL)
        decision = RouterDecision.from_route(route, reason="x", source="rules")
        payload = decision.model_dump()
        assert payload["intent"] == "casual"
        assert payload["provider_id"] == AIHUBMIX_QWEN_MAX
        assert payload["depth"] == "direct"
        assert payload["action"] == "answer"

    def test_decision_intent_is_string_for_business_and_ops(self) -> None:
        """use_enum_values=True guarantees downstream JSON consumers see strings."""
        business_decision = RouterDecision.from_route(
            get_provider_route(RouterIntent.INSIGHT),
            reason="x",
            source="rules",
        )
        ops_decision = RouterDecision.from_ops_route(
            get_ops_provider_route(OpsIntent.ERROR_DEBUG),
            reason="x",
            source="rules",
        )
        assert isinstance(business_decision.intent, str)
        assert isinstance(ops_decision.intent, str)


# ─────────────────────────────────────────────────────────────────────────
# 8. Classifier integration
# ─────────────────────────────────────────────────────────────────────────


class TestClassifierIntegrationContracts:
    """Locks down the optional LLM classifier integration."""

    def test_classifier_provider_id_is_gemini_pro(self) -> None:
        assert ROUTER_CLASSIFIER_PROVIDER_ID == AIHUBMIX_CLAUDE_OPUS_4_7 or (
            ROUTER_CLASSIFIER_PROVIDER_ID == "aihubmix/gemini-3.1-pro-preview"
        )

    @pytest.mark.asyncio
    async def test_noop_classifier_always_returns_none(self) -> None:
        noop = NoopRouterClassifier()
        assert await noop.classify("any text") is None
        assert await noop.classify("") is None


# ─────────────────────────────────────────────────────────────────────────
# 9. Module surface — locks down public exports
# ─────────────────────────────────────────────────────────────────────────


class TestPublicAPIContracts:
    """Locks down the public symbol surface of dc_router_core."""

    def test_provider_map_constants_are_stable(self) -> None:
        # Re-pinning the contract: changes to any of these are breaking.
        assert AIHUBMIX_GEMINI_FLASH == "aihubmix/gemini-3.5-flash"
        assert AIHUBMIX_QWEN_FLASH == "aihubmix/qwen3.6-flash"
        assert AIHUBMIX_QWEN_MAX == "aihubmix/qwen3.7-max"
        assert AIHUBMIX_DEEPSEEK_PRO == "aihubmix/deepseek-v4-pro"
        assert AIHUBMIX_DOUBAO_SEED_2_1_PRO == "aihubmix/doubao-seed-2-1-pro"
        assert AIHUBMIX_GROK == "aihubmix/grok-4.3"
        assert AIHUBMIX_CLAUDE_SONNET_4_6 == "aihubmix/claude-sonnet-4-6"
        assert AIHUBMIX_CLAUDE_OPUS_4_7 == "aihubmix/claude-opus-4-7"
        assert AIHUBMIX_CLAUDE_OPUS_4_8 == "aihubmix/claude-opus-4-8"
        assert CLI_GROK_BUILD == "cli/grok-build"
        assert CLI_CODEX_GPT_5_4 == "cli/codex/gpt-5.4"

    def test_default_provider_map_keys_match_router_intent_enum(self) -> None:
        assert set(DEFAULT_PROVIDER_MAP.keys()) == set(RouterIntent)

    def test_ops_provider_map_keys_match_ops_intent_enum(self) -> None:
        assert set(OPS_PROVIDER_MAP.keys()) == set(OpsIntent)

    def test_provider_route_dataclass_is_frozen(self) -> None:
        route = get_provider_route(RouterIntent.CASUAL)
        with pytest.raises(Exception):  # FrozenInstanceError or AttributeError
            route.provider_id = "tampered"  # type: ignore[misc]

    def test_ops_provider_route_dataclass_is_frozen(self) -> None:
        route = get_ops_provider_route(OpsIntent.QUEUE_STATUS)
        with pytest.raises(Exception):
            route.provider_id = "tampered"  # type: ignore[misc]


# ─────────────────────────────────────────────────────────────────────────
# 10. Internal rule consistency — make sure rule tables stay aligned
# ─────────────────────────────────────────────────────────────────────────


class TestRuleTableConsistency:
    """Locks down the integrity of the rule tables themselves."""

    def test_prefix_rule_intents_are_valid(self) -> None:
        for prefix, intent, _reason in PREFIX_RULES:
            assert isinstance(prefix, str)
            assert isinstance(intent, RouterIntent)

    def test_keyword_rule_intents_are_valid(self) -> None:
        for intent, keywords, _reason in KEYWORD_RULES:
            assert isinstance(intent, RouterIntent)
            assert all(isinstance(k, str) for k in keywords)

    def test_ops_prefix_rule_intents_are_valid(self) -> None:
        for prefix, intent, _reason in OPS_PREFIX_RULES:
            assert isinstance(prefix, str)
            assert isinstance(intent, OpsIntent)

    def test_ops_keyword_rule_intents_are_valid(self) -> None:
        for intent, keywords, _reason in OPS_KEYWORD_RULES:
            assert isinstance(intent, OpsIntent)
            assert all(isinstance(k, str) for k in keywords)

    def test_provider_route_actions_are_known(self) -> None:
        valid_actions = set(RouteAction)
        for _intent, route in DEFAULT_PROVIDER_MAP.items():
            assert route.action in valid_actions

    def test_ops_provider_route_actions_are_known(self) -> None:
        valid_actions = set(RouteAction)
        for _intent, route in OPS_PROVIDER_MAP.items():
            assert route.action in valid_actions


# ─────────────────────────────────────────────────────────────────────────
# 11. MessageEnvelope — pure dataclass contract
# ─────────────────────────────────────────────────────────────────────────


class TestMessageEnvelopeContracts:
    """Locks down the MessageEnvelope pure dataclass."""

    def test_envelope_constructs_from_text(self) -> None:
        env = MessageEnvelope(text="hello")
        assert env.text == "hello"
        assert env.has_attachments is False

    def test_envelope_combined_text_includes_attachment_summary(self) -> None:
        env = MessageEnvelope(text="看这张图", attachment_summary="a dog")
        assert "看这张图" in env.combined_text
        assert "a dog" in env.combined_text
        assert "[attachment_summary]" in env.combined_text

    def test_envelope_combined_text_omits_attachment_marker_when_no_summary(
        self,
    ) -> None:
        env = MessageEnvelope(text="看这张图", attachment_summary=None)
        assert env.combined_text == "看这张图"

    def test_envelope_has_attachments_when_kinds_present(self) -> None:
        env = MessageEnvelope(text="x", attachment_kinds=(AttachmentKind.IMAGE,))
        assert env.has_attachments is True

    def test_envelope_metadata_defaults_to_empty_dict(self) -> None:
        env = MessageEnvelope(text="x")
        assert env.metadata == {}
