"""Ops router 路由表 - 全用合规模型。

设计原则:
- 不引入 Claude OAuth (ToS 禁止) / 不引入 Gemini OAuth (灰色)
- 全部 depth=DIRECT (运维场景不需要深度队列 / Hermes 派发)
- 统一走 Codex CLI gpt-5.4，避免 DevOps 入口回到 Gemini CLI/OAuth 链路
"""

from __future__ import annotations

from dataclasses import dataclass

from dc_router.ops_taxonomy import OpsIntent
from dc_router.taxonomy import RouteAction, RouteDepth

OPS_CODEX_CLI = "cli/codex/gpt-5.4"


@dataclass(frozen=True, slots=True)
class OpsProviderRoute:
    intent: OpsIntent
    provider_id: str
    depth: RouteDepth
    action: RouteAction
    target_model: str | None = None
    description: str = ""


OPS_PROVIDER_MAP: dict[OpsIntent, OpsProviderRoute] = {
    OpsIntent.SYSTEM_STATUS: OpsProviderRoute(
        intent=OpsIntent.SYSTEM_STATUS,
        provider_id=OPS_CODEX_CLI,
        target_model="gpt-5.4",
        depth=RouteDepth.DIRECT,
        action=RouteAction.ANSWER,
        description="后台状态查询 - Codex CLI gpt-5.4",
    ),
    OpsIntent.QUEUE_STATUS: OpsProviderRoute(
        intent=OpsIntent.QUEUE_STATUS,
        provider_id=OPS_CODEX_CLI,
        target_model="gpt-5.4",
        depth=RouteDepth.DIRECT,
        action=RouteAction.ANSWER,
        description="队列/冷却状态查询 - Codex CLI gpt-5.4",
    ),
    OpsIntent.ERROR_DEBUG: OpsProviderRoute(
        intent=OpsIntent.ERROR_DEBUG,
        provider_id=OPS_CODEX_CLI,
        target_model="gpt-5.4",
        depth=RouteDepth.DIRECT,
        action=RouteAction.ANSWER,
        description="报错解释 - Codex CLI gpt-5.4",
    ),
    OpsIntent.CODE_OPS: OpsProviderRoute(
        intent=OpsIntent.CODE_OPS,
        provider_id=OPS_CODEX_CLI,
        target_model="gpt-5.4",
        depth=RouteDepth.DIRECT,
        action=RouteAction.ANSWER,
        description="小代码/小脚本 - Codex CLI gpt-5.4",
    ),
    OpsIntent.DEPLOYMENT_OPS: OpsProviderRoute(
        intent=OpsIntent.DEPLOYMENT_OPS,
        provider_id=OPS_CODEX_CLI,
        target_model="gpt-5.4",
        depth=RouteDepth.DIRECT,
        action=RouteAction.ANSWER,
        description="运维命令/部署 - Codex CLI gpt-5.4",
    ),
    OpsIntent.QUOTA_GATE_VIEW: OpsProviderRoute(
        intent=OpsIntent.QUOTA_GATE_VIEW,
        provider_id=OPS_CODEX_CLI,
        target_model="gpt-5.4",
        depth=RouteDepth.DIRECT,
        action=RouteAction.ANSWER,
        description="配额/凭证池/aihubmix 用量查看 - Codex CLI gpt-5.4",
    ),
    OpsIntent.OPS_FALLBACK: OpsProviderRoute(
        intent=OpsIntent.OPS_FALLBACK,
        provider_id=OPS_CODEX_CLI,
        target_model="gpt-5.4",
        depth=RouteDepth.DIRECT,
        action=RouteAction.ANSWER,
        description="运维场景兜底 - Codex CLI gpt-5.4",
    ),
}


def get_ops_provider_route(intent: OpsIntent) -> OpsProviderRoute:
    return OPS_PROVIDER_MAP.get(intent, OPS_PROVIDER_MAP[OpsIntent.OPS_FALLBACK])
