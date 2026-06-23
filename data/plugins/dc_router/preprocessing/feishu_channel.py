"""Feishu channel agent route — pinned provider per agent_id.

读取 ``data/config/dc_router_config.json::feishu_channel_routes`` 映射。
匹配时调 ``routing.apply_provider_pin`` 接管消息。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from astrbot.api import logger

from ..config import DCRouterConfig
from ..routing import apply_decision


def _route_for_event(event: Any, cfg: DCRouterConfig) -> dict[str, str] | None:
    get_extra = getattr(event, "get_extra", None)
    if not callable(get_extra):
        return None
    if get_extra("feishu_channel_allowed") is not True:
        return None
    agent_id = str(get_extra("feishu_channel_agent_id") or "").strip()
    if not agent_id:
        return None
    route = cfg.route_for_feishu_channel(agent_id)
    if route is None:
        return None
    return {
        **route,
        "agent_id": agent_id,
        "workspace": str(get_extra("feishu_channel_workspace") or "").strip(),
        "peer_kind": str(get_extra("feishu_channel_peer_kind") or "").strip(),
        "peer_id": str(get_extra("feishu_channel_peer_id") or "").strip(),
    }


async def try_apply_feishu_channel_route(
    context: Any,
    event: Any,
    cfg: DCRouterConfig,
) -> bool:
    """True 表示已成功接管。"""
    route = _route_for_event(event, cfg)
    if route is None:
        return False
    provider_id = route["provider_id"]
    if route.get("reasoning_tier"):
        try:
            event.set_extra("reasoning_tier", route["reasoning_tier"])
        except Exception:  # noqa: BLE001
            pass
    decision = SimpleNamespace(
        provider_id=provider_id,
        intent="feishu_channel_agent",
        depth="direct",
        action="feishu_channel_route",
        source="feishu_channel_control",
        metadata=route,
    )
    try:
        handled = await apply_decision(context, event, decision)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[dc_router] feishu channel route failed: %s", exc)
        return False
    if handled:
        try:
            event.set_extra("dc_router_feishu_agent_id", route["agent_id"])
            event.set_extra("dc_router_feishu_workspace", route["workspace"])
            event.set_extra("dc_router_feishu_peer_kind", route["peer_kind"])
            event.set_extra("dc_router_feishu_peer_id", route["peer_id"])
        except Exception:  # noqa: BLE001
            pass
        logger.info(
            "[dc_router] feishu channel route agent=%s provider=%s peer=%s:%s",
            route["agent_id"],
            provider_id,
            route["peer_kind"] or "-",
            route["peer_id"] or "-",
        )
    return handled


__all__ = ["try_apply_feishu_channel_route"]
