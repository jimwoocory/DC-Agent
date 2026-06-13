"""Backward-compat shim. The canonical home is `dc_router_core`.

Will be removed after the migration (see harness/contracts/routing_merge_contract.json).
"""

from dc_router_core.ops_rules import *  # noqa: F401,F403
from dc_router_core.ops_rules import (  # noqa: F401
    OPS_KEYWORD_RULES,
    OPS_PREFIX_RULES,
    OpsRuleMatch,
    match_ops_keywords,
    match_ops_prefix,
)
