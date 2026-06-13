"""Backward-compat shim. The canonical home is `dc_router_core`.

Will be removed after the migration (see harness/contracts/routing_merge_contract.json).
"""

from dc_router_core.ops_provider_map import *  # noqa: F401,F403
from dc_router_core.ops_provider_map import (  # noqa: F401
    OPS_CODEX_CLI,
    OPS_PROVIDER_MAP,
    OpsProviderRoute,
    get_ops_provider_route,
)
