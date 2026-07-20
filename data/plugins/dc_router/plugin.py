"""DC Router AstrBot Star plugin — slim entry point.

所有业务逻辑都已拆到 ``preprocessing/`` / ``routing/`` / ``cli_handlers.py`` /
``health.py``。本文件只剩 3 件事:

1. 注册 Star 元数据 (``@register("dc_router", ...)``)
2. 注册 ``@filter.event_message_type`` 钩子 → 调 ``dispatch.dispatch()``
3. 启动 / 停止 background queue recovery (optional)
"""

from __future__ import annotations

import asyncio

from astrbot.api import logger
from astrbot.api.star import Context, Star

from .config import load_config
from .dispatch import DispatchResult, dispatch
from .health import health_snapshot

__all__ = ["DCRouterPlugin", "health_snapshot", "dispatch", "DispatchResult"]


async def cancel_router_session_work(context, umo: str, *, reason: str) -> int:
    """Cancel router-owned media and quota jobs for one session.

    Args:
        context: Shared AstrBot runtime context.
        umo: Unified message origin identifying the session.
        reason: Cancellation reason forwarded to persistent workers.

    Returns:
        Total number of router-owned jobs cancelled.
    """
    cancelled = 0
    try:
        from .dc_quota_runtime import get_quota_gate

        gate = await get_quota_gate()
        cancelled += len(await gate.cancel_session_jobs(umo, reason=reason))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[dc_router] 取消 session quota jobs 失败: %s", exc)
    try:
        from .preprocessing.media_route import cancel_session_media_tasks

        cancelled += await cancel_session_media_tasks(
            context,
            umo,
            reason=reason,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[dc_router] 取消 session media jobs 失败: %s", exc)
    return cancelled


class DCRouterPlugin(Star):
    """Slim Star plugin — 全部业务逻辑收敛到 ``dispatch.dispatch()``。"""

    def __init__(self, context: Context) -> None:
        super().__init__(context)
        self._dc_queue_recovery_running = False
        self._dc_media_recovery_running = False
        self._dc_cancel_session_callback = None

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

    async def _stop_dc_queue_recovery(self) -> None:
        if not self._dc_queue_recovery_running:
            return
        try:
            from .cli_handlers import stop_queue_recovery
        except Exception:  # noqa: BLE001
            self._dc_queue_recovery_running = False
            return
        try:
            task = stop_queue_recovery()
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[dc_router] queue_recovery 停止忽略异常: %s", exc)
        finally:
            self._dc_queue_recovery_running = False

    def _start_dc_media_recovery(self) -> None:
        try:
            from .preprocessing.media_route import start_media_task_recovery
        except Exception as exc:  # noqa: BLE001
            logger.warning("[dc_router] 启动 media_task_recovery 失败: %s", exc)
            return
        try:
            start_media_task_recovery(self.context)
            self._dc_media_recovery_running = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("[dc_router] media_task_recovery 启动异常: %s", exc)

    async def _stop_dc_media_recovery(self) -> None:
        if not self._dc_media_recovery_running:
            return
        try:
            from .preprocessing.media_route import stop_media_task_recovery
        except Exception:  # noqa: BLE001
            self._dc_media_recovery_running = False
            return
        try:
            task = stop_media_task_recovery()
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[dc_router] media_task_recovery 停止忽略异常: %s", exc)
        finally:
            self._dc_media_recovery_running = False

    async def initialize(self) -> None:
        """Plugin 启动时只挂载 background helpers；不读取任何业务配置。"""
        cfg = load_config()
        if cfg.is_active and not cfg.is_dry_run:
            self._start_dc_queue_recovery()
            self._start_dc_media_recovery()

            async def _cancel_session(umo: str, *, reason: str) -> int:
                return await cancel_router_session_work(
                    self.context,
                    umo,
                    reason=reason,
                )

            self._dc_cancel_session_callback = _cancel_session
            self.context.dc_cancel_session_work = _cancel_session
        logger.info(
            "[dc_router] initialize · enabled=%s dry_run=%s architecture=%s "
            "main_agent=%s",
            cfg.enabled,
            cfg.dry_run,
            cfg.architecture_mode,
            cfg.main_agent_provider_id if cfg.uses_middle_router else "legacy",
        )

    async def terminate(self) -> None:
        if (
            getattr(self.context, "dc_cancel_session_work", None)
            is self._dc_cancel_session_callback
        ):
            self.context.dc_cancel_session_work = None
        self._dc_cancel_session_callback = None
        await self._stop_dc_queue_recovery()
        await self._stop_dc_media_recovery()
