"""AstrBot registration entry point for the DC router plugin."""

from __future__ import annotations

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.star import register

from .config import load_config
from .dispatch import dispatch
from .plugin import DCRouterPlugin as _DCRouterPluginBase


@register(
    "dc_router",
    "dc_agent",
    "DC 路由 · 业务 + DevOps 唯一意图分类与 provider 分发入口",
    "1.0.0",
)
class DCRouterPlugin(_DCRouterPluginBase):
    @filter.event_message_type(
        EventMessageType.GROUP_MESSAGE | EventMessageType.PRIVATE_MESSAGE
    )
    async def route(self, event: AstrMessageEvent) -> None:
        """所有消息的单一入口 — 委托给 ``dispatch.dispatch()``。"""
        cfg = load_config()
        try:
            result = await dispatch(self.context, event, cfg)
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "[dc_router] dispatch 异常，消息放行让默认 pipeline 处理: %s", exc
            )
            return
        if not result.handled:
            return
        logger.debug(
            "[dc_router] handled by source=%s intent=%s provider=%s",
            result.source,
            result.decision_intent or "-",
            result.decision_provider or "-",
        )
