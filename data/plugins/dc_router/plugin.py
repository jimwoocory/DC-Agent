"""DC Router AstrBot Star plugin — slim entry point.

所有业务逻辑都已拆到 ``preprocessing/`` / ``routing/`` / ``cli_handlers.py`` /
``health.py``。本文件只剩 3 件事:

1. 注册 Star 元数据 (``@register("dc_router", ...)``)
2. 注册 ``@filter.event_message_type`` 钩子 → 调 ``dispatch.dispatch()``
3. 启动 / 停止 background queue recovery (optional)
"""

from __future__ import annotations

from astrbot.api import logger
from astrbot.api.star import Context, Star

from .config import load_config
from .dispatch import DispatchResult, dispatch
from .health import health_snapshot

__all__ = ["DCRouterPlugin", "health_snapshot", "dispatch", "DispatchResult"]


class DCRouterPlugin(Star):
    """Slim Star plugin — 全部业务逻辑收敛到 ``dispatch.dispatch()``。"""

    def __init__(self, context: Context) -> None:
        super().__init__(context)
        self._dc_queue_recovery_running = False

    def _start_dc_queue_recovery(self) -> None:
        try:
            from .cli_handlers import start_queue_recovery
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[dc_router] 启动 queue_recovery 失败 (cli_handlers): %s", exc
            )
            return
        try:
            start_queue_recovery(self.context)
            self._dc_queue_recovery_running = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("[dc_router] queue_recovery 启动异常: %s", exc)

    def _stop_dc_queue_recovery(self) -> None:
        if not self._dc_queue_recovery_running:
            return
        try:
            from .cli_handlers import stop_queue_recovery
        except Exception:  # noqa: BLE001
            self._dc_queue_recovery_running = False
            return
        try:
            stop_queue_recovery()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[dc_router] queue_recovery 停止忽略异常: %s", exc)
        finally:
            self._dc_queue_recovery_running = False

    async def initialize(self) -> None:
        """Plugin 启动时只挂载 background helpers；不读取任何业务配置。"""
        cfg = load_config()
        if cfg.is_active and not cfg.is_dry_run:
            self._start_dc_queue_recovery()
        logger.info(
            "[dc_router] initialize · enabled=%s dry_run=%s",
            cfg.enabled,
            cfg.dry_run,
        )

    async def terminate(self) -> None:
        self._stop_dc_queue_recovery()
