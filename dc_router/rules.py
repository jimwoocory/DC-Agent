"""Backward-compat shim. The canonical home is `dc_router_core`.

Will be removed after the migration (see harness/contracts/routing_merge_contract.json).
"""

from dc_router_core.rules import *  # noqa: F401,F403
from dc_router_core.rules import (  # noqa: F401
    DEEP_TASK_RE,
    FEISHU_DOCUMENT_URL_RE,
    KEYWORD_RULES,
    PRD_TASK_RE,
    PREFIX_RULES,
    RuleMatch,
    match_document_link,
    match_keywords,
    match_prefix,
)
