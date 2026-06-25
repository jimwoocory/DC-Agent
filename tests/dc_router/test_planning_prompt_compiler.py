from __future__ import annotations

from dc_router_core.planning_prompt_compiler import build_planning_langgpt_prompt


def test_build_planning_langgpt_prompt_for_promotion_strategy() -> None:
    prompt = build_planning_langgpt_prompt(
        {
            "department_workflow": "planning_creative_fast",
            "planning_writing_mode": "promotion_strategy",
            "planning_structure_policy": "objective_audience_channels_content_calendar_kpi",
        }
    )

    assert "[LangGPT Structured Planning Prompt]" in prompt
    assert "# Role: 推广策略专业写作助手" in prompt
    assert "## Profile" in prompt
    assert "## Goal" in prompt
    assert "## Workflow" in prompt
    assert "渠道组合" in prompt
    assert "objective_audience_channels_content_calendar_kpi" in prompt


def test_build_planning_langgpt_prompt_for_market_report_requires_search() -> None:
    prompt = build_planning_langgpt_prompt(
        {
            "department_workflow": "planning_creative_fast",
            "planning_writing_mode": "market_research_report",
            "search_required": "true",
        }
    )

    assert "# Role: 市场调研报告专业写作助手" in prompt
    assert "This task requires web retrieval" in prompt
    assert "cite with exact <ref>index</ref>" in prompt
    assert "Do not invent data" in prompt


def test_build_planning_langgpt_prompt_ignores_non_planning_workflow() -> None:
    assert build_planning_langgpt_prompt({"department_workflow": "planning_direct_media"}) == ""
