"""Backward-compat shim. The canonical home is `dc_router_core`.

Will be removed after the migration (see harness/contracts/routing_merge_contract.json).
"""

from dc_router_core.provider_map import *  # noqa: F401,F403
from dc_router_core.provider_map import (  # noqa: F401 — explicit re-export
    DEFAULT_PROVIDER_MAP,
    ProviderRoute,
    get_provider_route,
)
