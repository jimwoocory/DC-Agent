"""Configuration loader for the dc_router AstrBot plugin."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

CONFIG_PATH = Path(
    os.environ.get(
        "DC_ROUTER_CONFIG_PATH",
        str(Path(get_astrbot_data_path()) / "config" / "dc_router_config.json"),
    )
)

# Legacy v1 fields are ignored with one log line if still present.
_LEGACY_KEYS = {"PREFIX_INTENTS", "INTENT_TO_PROVIDER", "REASONING_PREFIX_PROVIDERS"}

BUSINESS_PLATFORM_IDS: frozenset[str] = frozenset({"巅池-Agent小助手"})
OPS_PLATFORM_IDS: frozenset[str] = frozenset({"巅池-技术（DevOps）", "巅池-技术"})


@dataclass(slots=True)
class DCRouterConfig:
    enabled: bool = False
    dry_run: bool = True
    fallback_on_error: bool = True
    arbiter_enabled: bool = False
    """Enable L3 arbitration; false keeps PassThroughArbiter behavior."""
    classifier_enabled: bool = False
    """Enable LLM classifier for uncertain routing cases (fallback → classifier).
    Default False: unconfigured messages go straight to FALLBACK intent without
    the extra LLM call + latency. Enable only in staging/fallback config for
    manual verification before production rollout.
    """
    feishu_channel_routes: dict[str, Any] = field(default_factory=dict)
    queue_recovery_interval_seconds: int = 60
    config_path: Path = field(default_factory=lambda: CONFIG_PATH)
    last_loaded_at: float = 0.0

    @property
    def is_active(self) -> bool:
        """Return True only when dc_router really takes over dispatch."""
        return self.enabled and not self.dry_run

    @property
    def is_dry_run(self) -> bool:
        return self.enabled and self.dry_run

    def route_for_feishu_channel(self, agent_id: str) -> dict[str, str] | None:
        raw = self.feishu_channel_routes.get(agent_id)
        if raw is None:
            return None
        if isinstance(raw, str):
            provider_id = raw.strip()
            if not provider_id:
                return None
            return {"provider_id": provider_id, "reasoning_tier": ""}
        if isinstance(raw, dict):
            provider_id = str(raw.get("provider_id") or "").strip()
            if not provider_id:
                return None
            return {
                "provider_id": provider_id,
                "reasoning_tier": str(raw.get("reasoning_tier") or "").strip(),
            }
        return None


_SAFE_DEFAULTS = DCRouterConfig()


def load_config(path: Path | None = None) -> DCRouterConfig:
    """Read dc_router config, returning safe defaults on any parse/read error."""
    config_path = path or CONFIG_PATH
    try:
        if not config_path.exists():
            return DCRouterConfig(config_path=config_path)
        with config_path.open(encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return DCRouterConfig(config_path=config_path)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(
            "[dc_router] 配置 %s 解析失败，按安全默认处理: %s",
            config_path,
            exc,
        )
        return DCRouterConfig(config_path=config_path)

    # The file may parse as valid JSON but be a list / string / number
    # rather than the expected object. In that case ``.get`` does not
    # exist and would raise ``AttributeError`` — we MUST catch it
    # because the plugin runs on every message and a single bad edit
    # would cascade into message-processing failures.
    if not isinstance(data, dict):
        logger.warning(
            "[dc_router] 配置 %s 顶层不是 object (实际是 %s)，按安全默认处理",
            config_path,
            type(data).__name__,
        )
        return DCRouterConfig(config_path=config_path)

    legacy_hits = [k for k in _LEGACY_KEYS if k in data]
    if legacy_hits:
        logger.info(
            "[dc_router] 配置含 v1.0 死字段 %s，已忽略（仅 dc_router_core 路由表生效）",
            legacy_hits,
        )

    feishu_routes = data.get("feishu_channel_routes") or {}
    if not isinstance(feishu_routes, dict):
        feishu_routes = {}

    queue_interval = data.get("queue_recovery_interval_seconds", 60)
    if not isinstance(queue_interval, int) or queue_interval < 0:
        queue_interval = 60

    return DCRouterConfig(
        enabled=bool(data.get("enabled", False)),
        dry_run=bool(data.get("dry_run", True)),
        fallback_on_error=bool(data.get("fallback_on_error", True)),
        arbiter_enabled=bool(data.get("arbiter_enabled", False)),
        classifier_enabled=bool(data.get("classifier_enabled", False)),
        feishu_channel_routes=feishu_routes,
        queue_recovery_interval_seconds=queue_interval,
        config_path=config_path,
        last_loaded_at=os.path.getmtime(config_path),
    )


def is_business_platform(platform_id: str) -> bool:
    return platform_id in BUSINESS_PLATFORM_IDS


def is_ops_platform(platform_id: str) -> bool:
    return platform_id in OPS_PLATFORM_IDS


def is_dc_router_managed_platform(platform_id: str) -> bool:
    return platform_id in (BUSINESS_PLATFORM_IDS | OPS_PLATFORM_IDS)


__all__ = [
    "DCRouterConfig",
    "load_config",
    "is_business_platform",
    "is_ops_platform",
    "is_dc_router_managed_platform",
    "BUSINESS_PLATFORM_IDS",
    "OPS_PLATFORM_IDS",
]
