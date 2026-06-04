"""Router decision contract.

为支持双 router (business + ops)，intent 字段类型放宽到 str：
- business 路径塞 RouterIntent.value
- ops 路径塞 OpsIntent.value
两种 enum 都通过 pydantic ConfigDict(use_enum_values=True) 转字符串存储。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from router.provider_map import ProviderRoute
from router.taxonomy import RouteAction, RouteDepth


class RouterDecision(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    intent: str
    """业务意图字符串。值来自 RouterIntent (business) 或 OpsIntent (ops)。"""

    depth: RouteDepth
    action: RouteAction
    provider_id: str
    target_model: str | None = None
    resource_keys: tuple[str, ...] = ()
    requires_queue: bool = False
    requires_harness: bool = False
    needs_multimodal_preprocess: bool = False
    source: str = "rules"
    reason: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_route(
        cls,
        route: ProviderRoute,
        *,
        reason: str,
        source: str,
        needs_multimodal_preprocess: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> RouterDecision:
        return cls(
            intent=route.intent.value
            if hasattr(route.intent, "value")
            else str(route.intent),
            depth=route.depth,
            action=route.action,
            provider_id=route.provider_id,
            target_model=route.target_model,
            resource_keys=route.resource_keys,
            requires_queue=route.requires_queue,
            requires_harness=route.requires_harness,
            needs_multimodal_preprocess=needs_multimodal_preprocess,
            source=source,
            reason=reason,
            metadata=metadata or {},
        )

    @classmethod
    def from_ops_route(
        cls,
        route: Any,
        *,
        reason: str,
        source: str,
        metadata: dict[str, Any] | None = None,
    ) -> RouterDecision:
        """Ops router 专用工厂。

        OpsProviderRoute 字段比 business ProviderRoute 少（不需要 resource_keys /
        requires_queue / requires_harness），用默认值填充。
        """
        return cls(
            intent=route.intent.value
            if hasattr(route.intent, "value")
            else str(route.intent),
            depth=route.depth,
            action=route.action,
            provider_id=route.provider_id,
            target_model=route.target_model,
            resource_keys=(),
            requires_queue=False,
            requires_harness=False,
            needs_multimodal_preprocess=False,
            source=source,
            reason=reason,
            metadata=metadata or {},
        )
