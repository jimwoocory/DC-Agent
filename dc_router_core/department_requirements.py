"""Hard-coded department workflow requirements for business routing."""

from __future__ import annotations

import re
from dataclasses import dataclass

from dc_router_core.taxonomy import RouterIntent


@dataclass(frozen=True, slots=True)
class DepartmentRequirement:
    intent: RouterIntent
    reason: str
    workflow: str
    metadata: dict[str, str]


_PLANNING_DEPARTMENT_RE = re.compile(
    r"(策划部|策划|策略部|策略|中台策略部|中台策划|中台策略|规划|planning|strategy)",
    re.IGNORECASE,
)
_CLIENT_DEPARTMENT_RE = re.compile(
    r"(客户部|中台客户部|中台客户|中台-客户|客户侧|业务部|商务市场)",
    re.IGNORECASE,
)
_MIDDLE_PLATFORM_STRATEGY_WORK_RE = re.compile(
    r"(中台).{0,12}(账号运营|用户运营|栏目|直播节奏|内容节奏|企微内容库)",
    re.IGNORECASE,
)
_EXECUTION_WORK_RE = re.compile(
    r"(执行分工表|项目启动会|落地沟通会|落地执行|执行排期|执行计划|"
    r"场地|勘场|场地规划图|物料|安装|点检|拍照验收|验收材料|"
    r"礼品打样|生产工厂|大货|抽检|入库|出库|库存)",
    re.IGNORECASE,
)
_DESIGN_WORK_RE = re.compile(
    r"(设计稿|VI|色调|字体|背景|尺寸比例|视觉检查|设计交付|制作文件|源文件|排版)",
    re.IGNORECASE,
)
_FILM_WORK_RE = re.compile(
    r"(拍摄通告|机位|道化服|现场拍摄|跟拍|后期剪辑|成片|成片交付|拍摄重点)",
    re.IGNORECASE,
)
_AI_APPLICATION_WORK_RE = re.compile(
    r"(AI应用部|AI应用|AI工具|自动化|知识库接入|机器人配置|小助手配置|部门小助手|工具选型|使用培训)",
    re.IGNORECASE,
)
_BRAND_PUBLICITY_CONTEXT_RE = re.compile(
    r"(品宣部|品宣|品牌宣传|宣发|运营部|媒介|KOC|koc|用户故事|企微|企业微信|社群|直播运营|账号运营)",
    re.IGNORECASE,
)
_BRAND_PUBLICITY_WORK_RE = re.compile(
    r"(直播运营|账号运营|内容运营|起号|爆品|引流品|周复盘|月复盘|"
    r"媒介|KOC|koc|达人|任务下发|需求管理表|日报|反馈闭环|"
    r"用户故事|用户故事库|用户活动|沟通脚本|甲方审核|成片审核|用户证言|"
    r"企微运营|企业微信|企微|私域内容库|社群维护|风险预警|负面稀释|竞品负面)",
    re.IGNORECASE,
)
_LIUQI_BRANCH_RE = re.compile(r"(柳汽)", re.IGNORECASE)
_PLANNING_WORK_RE = re.compile(
    r"(活动方案|执行文档|方案框架|框架梳理|创意|栏目封面|视频栏目封面|"
    r"节气海报|海报|推文长图|风格模拟|礼品|同系列|方案需求|传播方案|"
    r"推广策略|方案策划|广告创意|广告创意文案|市场调研报告)",
    re.IGNORECASE,
)
_PLANNING_DIRECT_MEDIA_RE = re.compile(
    r"(生成|做|出|来|要|想要|需要|帮我).{0,18}(一张|一个|份)?.{0,8}"
    r"(海报|图片|封面|长图|视觉|生图)",
    re.IGNORECASE,
)
_PLANNING_CREATIVE_FAST_RE = re.compile(
    r"(方案框架|框架搭建|框架梳理|活动执行文档|活动方案|创意方向|"
    r"创意文案|海报思路|栏目封面|视频栏目封面|节气海报|推文长图|"
    r"风格模拟|礼品规划|同系列礼品|短视频脚本|视频脚本|分镜|旁白|slogan|广告语|"
    r"推广策略|方案策划|广告创意|广告创意文案|市场调研报告)",
    re.IGNORECASE,
)
_PLANNING_FRAMEWORK_RE = re.compile(
    r"(方案框架|框架搭建|框架梳理|框架|目录|大纲|PPT|ppt|小标题|提案结构|方案结构)",
    re.IGNORECASE,
)
_PLANNING_RESEARCH_RE = re.compile(
    r"(竞品|行业趋势|趋势|抖音|公众号|网站|小红书|用户反馈|线上反馈|"
    r"负面关键词|负面评论|评论|舆情|搜索|检索)",
    re.IGNORECASE,
)
_PUBLIC_OPINION_RE = re.compile(
    r"(负面关键词|负面评论|用户反馈|线上反馈|评论|舆情|危机)",
    re.IGNORECASE,
)
_PROMPT_BRIEF_RE = re.compile(r"(prompt|提示词|brief|思路)", re.IGNORECASE)
_CROSS_DEPARTMENT_COORDINATION_RE = re.compile(
    r"(怎么分工|如何分工|谁来|谁负责|边界|职责|冲突|都提到|同时提到|一起提到)",
    re.IGNORECASE,
)
_LIUQI_ORG_INTERFACE_RE = re.compile(
    r"(组织接口|保留接口|只留接口|接口|外派|独立分支|分支|先保留)",
    re.IGNORECASE,
)

_PLANNING_WRITING_MODES: tuple[tuple[str, str, str, str], ...] = (
    (
        "promotion_strategy",
        "推广策略",
        r"(推广策略|推广方案|营销推广|传播策略|渠道推广|推广节奏|投放策略)",
        "objective_audience_channels_content_calendar_kpi",
    ),
    (
        "proposal_planning",
        "方案策划",
        r"(方案策划|活动方案|执行方案|落地方案|方案框架|框架梳理|方案结构|策划案)",
        "background_objective_strategy_execution_timeline_deliverables_risk",
    ),
    (
        "ad_creative",
        "广告创意",
        r"(广告创意|广告创意文案|创意文案|广告语|slogan|卖点文案|传播口号)",
        "insight_big_idea_copy_variants_visual_direction_channel_adaptation",
    ),
    (
        "market_research_report",
        "市场调研报告",
        r"(市场调研报告|市场调研|调研报告|市场洞察|行业洞察|竞品分析|用户洞察)",
        "scope_sources_market_competition_users_opportunities_recommendations",
    ),
)
_PLANNING_WRITING_MODE_RE: tuple[tuple[str, str, re.Pattern[str], str], ...] = tuple(
    (
        mode_id,
        label,
        re.compile(pattern, re.IGNORECASE),
        structure_policy,
    )
    for mode_id, label, pattern, structure_policy in _PLANNING_WRITING_MODES
)


def match_department_requirement(
    text: str,
    *,
    metadata: dict[str, str] | None = None,
) -> DepartmentRequirement | None:
    """Return the business workflow requirement that must override generic SOP.

    Planning colleagues primarily need fast creative execution. Their everyday
    creative/image/framework requests should not be dragged into deep company
    memory scans or multi-step material intake unless the user explicitly asks
    for review, facts, or source-backed research.
    """
    metadata = metadata or {}
    combined = _combined_department_text(text, metadata)
    if _is_cross_department_coordination_question(combined):
        return None
    if _is_liuqi_org_interface_context(text):
        return None

    specialized_execution_requirement = _match_execution_subdepartment_requirement(text)
    if specialized_execution_requirement:
        return specialized_execution_requirement

    execution_requirement = _match_execution_requirement(text)
    if execution_requirement:
        return execution_requirement

    brand_publicity_requirement = _match_brand_publicity_requirement(text, combined)
    if brand_publicity_requirement:
        return brand_publicity_requirement

    planning_context = _is_planning_context(combined)
    client_context = bool(_CLIENT_DEPARTMENT_RE.search(combined))
    writing_mode = _match_planning_writing_mode(text)
    planning_research = _PLANNING_RESEARCH_RE.search(text) and writing_mode is None
    if client_context and not _PLANNING_DEPARTMENT_RE.search(combined):
        return None
    if not planning_context and not planning_research:
        return None

    if planning_research:
        intent = (
            RouterIntent.PUBLIC_OPINION
            if _PUBLIC_OPINION_RE.search(text)
            else RouterIntent.REALTIME
        )
        return DepartmentRequirement(
            intent=intent,
            reason="Matched hard-coded planning department source-backed research need.",
            workflow="planning_research_with_sources",
            metadata={
                "department": "planning",
                "department_workflow": "planning_research_with_sources",
                "search_required": "true",
                "source_policy": "separate_facts_from_assumptions",
                "answer_policy": "cite_sources_or_state_unavailable",
            },
        )

    if _PLANNING_DIRECT_MEDIA_RE.search(text) and not _PROMPT_BRIEF_RE.search(text):
        return DepartmentRequirement(
            intent=RouterIntent.CREATIVE,
            reason="Matched hard-coded planning department direct media need.",
            workflow="planning_direct_media",
            metadata={
                "department": "planning",
                "department_workflow": "planning_direct_media",
                "answer_policy": "generate_directly_no_prompt_coaching",
                "clarification_policy": "avoid_required_followups",
                "material_policy": "optional_for_first_draft",
                "media_policy": "direct_generation",
                "default_aspect_ratio": "portrait",
            },
        )

    planning_framework = _PLANNING_FRAMEWORK_RE.search(text)
    if (
        _PLANNING_CREATIVE_FAST_RE.search(text)
        or _PLANNING_WORK_RE.search(text)
        or _MIDDLE_PLATFORM_STRATEGY_WORK_RE.search(combined)
    ):
        metadata = {
            "department": "planning",
            "department_workflow": "planning_creative_fast",
            "planning_writing_skill": "planning-writing",
            "answer_policy": "answer_first",
            "clarification_policy": "max_one_optional_followup",
            "memory_policy": "skip_deep_company_memory_by_default",
            "material_policy": "optional_for_first_draft",
            "latency_policy": "fast_first_draft",
        }
        if writing_mode is not None:
            mode_id, label, structure_policy = writing_mode
            metadata.update(
                {
                    "planning_writing_mode": mode_id,
                    "planning_writing_mode_label": label,
                    "planning_structure_policy": structure_policy,
                }
            )
            if mode_id == "market_research_report":
                metadata.update(
                    {
                        "search_required": "true",
                        "source_policy": "separate_facts_from_assumptions",
                        "report_policy": "source_backed_market_research_report",
                    }
                )
        if planning_framework:
            metadata.update(
                {
                    "planning_output_mode": "framework_first",
                    "framework_policy": "toc_then_slide_titles_then_page_details",
                    "structure_policy": "standardized_hierarchical_closed_loop",
                    "ppt_policy": "each_page_has_title_and_supporting_points",
                }
            )
        return DepartmentRequirement(
            intent=RouterIntent.CREATIVE,
            reason="Matched hard-coded planning department fast creative need.",
            workflow="planning_creative_fast",
            metadata=metadata,
        )

    return None


def _match_execution_subdepartment_requirement(
    text: str,
) -> DepartmentRequirement | None:
    if _DESIGN_WORK_RE.search(text):
        return _work_preflight_requirement(
            department="design_dept",
            workflow="design_delivery",
            reason="Matched hard-coded design department delivery workflow need.",
            output_policy="visual_review_production_risks_revision_handoff",
        )
    if _FILM_WORK_RE.search(text):
        return _work_preflight_requirement(
            department="film_production",
            workflow="film_production_delivery",
            reason="Matched hard-coded film production delivery workflow need.",
            output_policy="shooting_notice_camera_plan_post_acceptance",
        )
    if _AI_APPLICATION_WORK_RE.search(text):
        return _work_preflight_requirement(
            department="ai_application",
            workflow="ai_application_enablement",
            reason="Matched hard-coded AI application workflow need.",
            output_policy="workflow_tooling_data_boundary_rollout_training",
        )
    return None


def _match_execution_requirement(
    text: str,
) -> DepartmentRequirement | None:
    execution_work = bool(_EXECUTION_WORK_RE.search(text))
    if not execution_work:
        return None
    return _work_preflight_requirement(
        department="execution_ops",
        workflow="execution_delivery",
        reason="Matched hard-coded activity coordination delivery workflow need.",
        output_policy="tasks_timeline_owner_risks_acceptance",
    )


def _match_brand_publicity_requirement(
    text: str,
    combined: str,
) -> DepartmentRequirement | None:
    if _LIUQI_BRANCH_RE.search(combined):
        return None
    if (
        _PLANNING_DEPARTMENT_RE.search(combined)
        or _MIDDLE_PLATFORM_STRATEGY_WORK_RE.search(combined)
        or _CLIENT_DEPARTMENT_RE.search(combined)
    ):
        return None
    if _PLANNING_DIRECT_MEDIA_RE.search(text) and not _PROMPT_BRIEF_RE.search(text):
        return None
    has_brand_context = bool(_BRAND_PUBLICITY_CONTEXT_RE.search(combined))
    has_brand_work = bool(_BRAND_PUBLICITY_WORK_RE.search(text))
    if not has_brand_context or not has_brand_work:
        return None
    return _work_preflight_requirement(
        department="brand_publicity",
        workflow="brand_publicity_ops",
        reason="Matched hard-coded brand publicity operations workflow need.",
        output_policy="content_calendar_kpi_koc_story_community_risks",
        material_policy="ask_missing_brand_publicity_facts",
        extra_metadata={
            "branch_policy": "liuqi_placeholder_only",
            "answer_policy": "operations_brief_first",
        },
    )


def _work_preflight_requirement(
    *,
    department: str,
    workflow: str,
    reason: str,
    output_policy: str,
    material_policy: str = "ask_missing_execution_facts",
    extra_metadata: dict[str, str] | None = None,
) -> DepartmentRequirement:
    metadata = {
        "department": department,
        "department_workflow": workflow,
        "answer_policy": "checklist_first",
        "output_policy": output_policy,
        "material_policy": material_policy,
        "queue_policy": "no_queue_without_resource_reason",
    }
    if extra_metadata:
        metadata.update(extra_metadata)
    return DepartmentRequirement(
        intent=RouterIntent.WORK_PREFLIGHT,
        reason=reason,
        workflow=workflow,
        metadata=metadata,
    )


def _combined_department_text(text: str, metadata: dict[str, str]) -> str:
    department_bits = [
        metadata.get("department", ""),
        metadata.get("requester_department", ""),
        metadata.get("requester_business_department", ""),
        metadata.get("requester_department_path", ""),
        metadata.get("requester_department_aliases", ""),
    ]
    return "\n".join([text, *department_bits])


def _is_planning_context(text: str) -> bool:
    return bool(
        _PLANNING_DEPARTMENT_RE.search(text)
        or _MIDDLE_PLATFORM_STRATEGY_WORK_RE.search(text)
        or _PLANNING_WORK_RE.search(text)
    )


def _is_cross_department_coordination_question(text: str) -> bool:
    has_brand = bool(_BRAND_PUBLICITY_CONTEXT_RE.search(text))
    has_planning_or_middle = bool(
        _PLANNING_DEPARTMENT_RE.search(text)
        or _MIDDLE_PLATFORM_STRATEGY_WORK_RE.search(text)
    )
    return (
        has_brand
        and has_planning_or_middle
        and bool(_CROSS_DEPARTMENT_COORDINATION_RE.search(text))
    )


def _is_liuqi_org_interface_context(text: str) -> bool:
    return bool(_LIUQI_BRANCH_RE.search(text) and _LIUQI_ORG_INTERFACE_RE.search(text))


def _match_planning_writing_mode(text: str) -> tuple[str, str, str] | None:
    for mode_id, label, pattern, structure_policy in _PLANNING_WRITING_MODE_RE:
        if pattern.search(text):
            return mode_id, label, structure_policy
    return None


__all__ = ["DepartmentRequirement", "match_department_requirement"]
