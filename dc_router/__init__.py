"""DC router package — backward-compat shim.

Canonical home is `dc_router_core`. This package re-exports its public API for
existing callers (e.g. `data/plugins/llm_router/dc_router_adapter.py`,
`dc_router/tests/`, harness contract callers). It will be deleted in phase 5 of
the routing merge (see harness/contracts/routing_merge_contract.json).
"""

from dc_router_core.decision import RouterDecision
from dc_router_core.entrypoint import DCRouter, MessageEnvelope
from dc_router_core.ops_taxonomy import OpsIntent
from dc_router_core.taxonomy import (
    AttachmentKind,
    RouteAction,
    RouteDepth,
    RouterIntent,
)

__all__ = [
    "AttachmentKind",
    "DCRouter",
    "MessageEnvelope",
    "OpsIntent",
    "RouteAction",
    "RouteDepth",
    "RouterDecision",
    "RouterIntent",
]
