"""DC middle Router AstrBot Star plugin.

业务逻辑分布在:

- ``preprocessing/`` — 平台级预处理 (chitchat / card / dept memory / tone /
  media / feishu channel)
- ``middle_router_adapter.py`` — Agent/menu/card shared deterministic Interface
- ``routing/``       — legacy compatibility logic (envelope / reasoning prefix /
  apply decision / v1.0 fallback)
- ``cli_handlers.py`` — CLI provider 编排 (codex / grok); legacy CLI ids are disabled
- ``health.py``      — routed provider health snapshot
- ``config.py``      — 单一来源 (data/config/dc_router_config.json)
- ``dispatch.py``    — access adapters plus middle/legacy architecture switch
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
