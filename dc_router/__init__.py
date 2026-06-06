"""DC router package.

This package is intentionally independent from AstrBot's legacy router. The
AstrBot entrypoint should call this package as the only routing authority once
the new path is enabled.

双 router 架构 (按 envelope.metadata['platform_id'] 自动切):
- business: RouterIntent / provider_map / rules        (员工业务入口)
- ops:      OpsIntent     / ops_provider_map / ops_rules (DevOps 机器人入口)

两套路由表完全独立, 输出共用 RouterDecision 契约。
"""

from dc_router.decision import RouterDecision
from dc_router.entrypoint import DCRouter, MessageEnvelope
from dc_router.ops_taxonomy import OpsIntent
from dc_router.taxonomy import AttachmentKind, RouteAction, RouteDepth, RouterIntent

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
