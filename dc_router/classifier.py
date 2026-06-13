"""Backward-compat shim. The canonical home is `dc_router_core`.

Will be removed after the migration (see harness/contracts/routing_merge_contract.json).
"""

from dc_router_core.classifier import *  # noqa: F401,F403
from dc_router_core.classifier import (  # noqa: F401
    ROUTER_CLASSIFIER_PROVIDER_ID,
    ROUTER_CLASSIFIER_SYSTEM_PROMPT,
    ClassifierResult,
    NoopRouterClassifier,
    RouterClassifier,
)
