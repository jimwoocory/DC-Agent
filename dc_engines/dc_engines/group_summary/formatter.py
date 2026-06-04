"""Render a GroupSummary into the markdown block sent back to the group.

Output shape (matches W1 / 2A-1 acceptance criterion):

    ## 群聊总结  ｜  最近 N 条 ｜  YYYY-MM-DD HH:MM ~ HH:MM

    ### 客户反馈
    {client_feedback}

    ### 老板要求
    {boss_requests}

    ### 执行问题
    {execution_issues}

    ### 阶段结论
    {stage_conclusions}
"""

from __future__ import annotations

from .contracts import GroupSummary


def _section(title: str, body: str) -> str:
    body = (body or "").strip() or "（无）"
    return f"### {title}\n{body}"


def format_summary(summary: GroupSummary) -> str:
    """Render the summary as a markdown string ready for MessageChain."""
    header = f"## 群聊总结  ｜  {summary.message_count} 条  ｜  {summary.time_range}"
    sections = [
        _section("客户反馈", summary.client_feedback),
        _section("老板要求", summary.boss_requests),
        _section("执行问题", summary.execution_issues),
        _section("阶段结论", summary.stage_conclusions),
    ]
    return "\n\n".join([header, *sections])
