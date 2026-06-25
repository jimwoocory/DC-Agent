"""LangGPT-style prompt compiler for planning department writing workflows."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PlanningWritingModeSpec:
    mode: str
    label: str
    skill_name: str
    output_contract: tuple[str, ...]


_MODE_SPECS: dict[str, PlanningWritingModeSpec] = {
    "promotion_strategy": PlanningWritingModeSpec(
        mode="promotion_strategy",
        label="推广策略",
        skill_name="推广策略制定",
        output_contract=(
            "策略总览：目标、人群、核心打法",
            "用户与场景洞察：需求、痛点、决策触点",
            "渠道组合：主阵地、辅助渠道、分发节奏",
            "内容策略：主张、栏目、样本文案方向",
            "执行节奏：预热期、爆发期、长尾期",
            "KPI 与复盘：曝光、互动、转化、优化机制",
        ),
    ),
    "proposal_planning": PlanningWritingModeSpec(
        mode="proposal_planning",
        label="方案策划",
        skill_name="方案策划与落地拆解",
        output_contract=(
            "项目背景",
            "目标与边界",
            "核心策略",
            "方案框架：一级目录、每页/每模块小标题、支撑内容",
            "执行路径：时间节点、责任分工、关键动作",
            "物料清单、风险预案、验收标准",
        ),
    ),
    "ad_creative": PlanningWritingModeSpec(
        mode="ad_creative",
        label="广告创意",
        skill_name="广告创意与传播文案",
        output_contract=(
            "创意洞察",
            "核心 Big Idea",
            "传播主张",
            "文案方向：主标题、副标题、短句版、社媒版",
            "视觉方向：画面主体、情绪氛围、构图建议",
            "渠道适配：抖音/视频号、小红书、公众号、海报/长图",
        ),
    ),
    "market_research_report": PlanningWritingModeSpec(
        mode="market_research_report",
        label="市场调研报告",
        skill_name="市场调研报告写作",
        output_contract=(
            "调研目标与范围",
            "信息来源：已有资料、联网检索结果、待确认假设",
            "市场现状",
            "竞品/标杆分析",
            "用户洞察",
            "机会点判断",
            "策略建议与后续验证计划",
        ),
    ),
}

_DEFAULT_SPEC = PlanningWritingModeSpec(
    mode="planning_general",
    label="策划写作",
    skill_name="策划部结构化写作",
    output_contract=(
        "先给目录/框架",
        "再给每一部分的关键内容",
        "最后给执行清单、风险与待确认信息",
    ),
)


def build_planning_langgpt_prompt(metadata: Mapping[str, object]) -> str:
    """Build a bounded LangGPT-style system prompt for planning writing tasks."""
    if str(metadata.get("department_workflow") or "").strip() != "planning_creative_fast":
        return ""

    mode = str(metadata.get("planning_writing_mode") or "").strip()
    spec = _MODE_SPECS.get(mode, _DEFAULT_SPEC)
    search_required = _truthy(metadata.get("search_required"))
    structure_policy = str(metadata.get("planning_structure_policy") or "").strip()

    lines = [
        "",
        "[LangGPT Structured Planning Prompt]",
        f"# Role: {spec.label}专业写作助手",
        "",
        "## Profile",
        "- Language: 中文",
        "- Domain: 汽车/社媒/活动/电商/品牌传播等策划部日常工作",
        "- Style: 专业、清晰、可落地，适合直接进入 PPT 或执行文档",
        "- Method: LangGPT-style structured prompt with Role/Profile/Goal/Skills/Rules/Workflow/Output",
        "",
        "## Goal",
        f"- Produce a complete first draft for `{spec.label}`.",
        "- Make the structure clear enough that planning colleagues can directly revise, copy, or拆成 PPT 页面。",
        "- Ask at most one optional follow-up after the draft, never before the draft.",
        "",
        "## Skills",
        f"### Skill-1: {spec.skill_name}",
        *[f"- {item}" for item in spec.output_contract],
        "",
        "### Skill-2: 框架化表达",
        "- 先搭目录，再展开模块，再落到动作、责任、物料、节奏和验收。",
        "- 每一层标题必须能回答“为什么做、做什么、怎么做、如何验证”。",
        "",
        "## Rules",
        "1. Do not teach the user how to write prompts; directly produce the draft.",
        "2. Do not invent data, rankings, dates, prices, policies, traffic numbers, or source names.",
        "3. Separate verified facts, assumptions, and recommendations when factual support is incomplete.",
        "4. Keep output practical: avoid empty adjectives and generic slogans without execution meaning.",
        "5. Prefer tables only when they improve execution clarity.",
    ]
    if search_required:
        lines.extend(
            [
                "6. This task requires web retrieval. Use search results as locked evidence, cite with exact <ref>index</ref>, and say when facts cannot be verified.",
            ]
        )
    if structure_policy:
        lines.append(f"7. Required structure policy: {structure_policy}.")

    lines.extend(
        [
            "",
            "## Workflow",
            "1. Identify the writing mode and business objective.",
            "2. Extract known inputs from the user message and attachments.",
            "3. If search is required, call the configured web search tool before final answer.",
            "4. Draft the document using the output contract above.",
            "5. Add a short `待确认信息` section only for material gaps that affect accuracy.",
            "",
            "## Output",
            "- Use Markdown headings.",
            "- Start with `# ` plus a concrete document title.",
            "- Then provide the full structured draft.",
            "- End with `待确认信息` and `下一步建议` when useful.",
            "",
            "## Initialization",
            "Follow this role, rules, workflow, and output contract for the current request.",
        ]
    )
    return "\n".join(lines) + "\n"


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


__all__ = ["PlanningWritingModeSpec", "build_planning_langgpt_prompt"]
