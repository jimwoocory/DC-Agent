"""Backward-compat shim. The canonical home is `dc_router_core`.

Will be removed after the migration (see harness/contracts/routing_merge_contract.json).
"""

from dc_router_core.entrypoint import *  # noqa: F401,F403
from dc_router_core.entrypoint import (  # noqa: F401
    ArbitrationResult,
    DCRouter,
    MessageEnvelope,
    PassThroughArbiter,
    RouteArbiter,
)
