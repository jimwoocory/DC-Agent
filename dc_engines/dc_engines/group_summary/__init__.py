"""Group chat summary skill (W1 / 2A-1).

Triggered when a user @-mentions DC-Agent in a project group together with a
summary keyword (e.g. "总结今天", "summary chat"). The skill pulls recent
group messages via the platform-native API, asks the LLM to bucket them into
four sections (client feedback / boss requests / execution issues / stage
conclusions) and renders the result back into the group.

Entry point lives in ``astrbot.core.pipeline.process_stage.router_stage``
(``_handle_group_summary``); this package only exposes pure-ish building
blocks so they can be unit-tested without spinning up the full pipeline.
"""

from .contracts import GroupMessage, GroupSummary
from .formatter import format_summary
from .history_fetcher import fetch_group_messages
from .summarizer import GROUP_SUMMARY_SYSTEM_PROMPT, summarize_group_chat
from .time_range import TimeRange, parse_time_range

__all__ = [
    "GROUP_SUMMARY_SYSTEM_PROMPT",
    "GroupMessage",
    "GroupSummary",
    "TimeRange",
    "fetch_group_messages",
    "format_summary",
    "parse_time_range",
    "summarize_group_chat",
]
