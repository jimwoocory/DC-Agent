"""DC 路由 AstrBot Star 插件 (业务 + DevOps 唯一路由入口).

业务逻辑分布在:

- ``preprocessing/`` — 平台级预处理 (chitchat / card / dept memory / tone /
  media / feishu channel)
- ``routing/``       — 纯路由逻辑 (envelope / reasoning prefix / apply decision
  / legacy v1.0 fallback)
- ``cli_handlers.py`` — CLI provider 编排 (codex / grok); legacy CLI ids are disabled
- ``health.py``      — routed provider health snapshot
- ``config.py``      — 单一来源 (data/config/dc_router_config.json)
- ``dispatch.py``    — 编排层 (顺序敏感: card → slash → chitchat → reasoning
  prefix → feishu → truth intake → dept memory → memory injection → tone →
  media → dc_router → v1.0 fallback)
- ``plugin.py``      — Star 入口 (DCRouterPlugin + @filter hook)
"""

from .config import (
    BUSINESS_PLATFORM_IDS,
    OPS_PLATFORM_IDS,
    DCRouterConfig,
    is_business_platform,
    is_dc_router_managed_platform,
    is_ops_platform,
    load_config,
)
from .main import DCRouterPlugin
from .plugin import DispatchResult, dispatch, health_snapshot

__all__ = [
    "BUSINESS_PLATFORM_IDS",
    "DCRouterConfig",
    "DCRouterPlugin",
    "DispatchResult",
    "OPS_PLATFORM_IDS",
    "dispatch",
    "health_snapshot",
    "is_business_platform",
    "is_dc_router_managed_platform",
    "is_ops_platform",
    "load_config",
]
