"""AstrBot 启动器，负责初始化和启动核心组件和仪表板服务器。

工作流程:
1. 初始化核心生命周期, 传递数据库和日志代理实例到核心生命周期
2. 运行核心生命周期任务和仪表板服务器
"""

import asyncio
import signal
import traceback

from astrbot.core import LogBroker, LogManager, logger
from astrbot.core.core_lifecycle import AstrBotCoreLifecycle
from astrbot.core.db import BaseDatabase
from astrbot.dashboard.server import AstrBotDashboard


class InitialLoader:
    """AstrBot 启动器，负责初始化和启动核心组件和仪表板服务器。"""

    def __init__(self, db: BaseDatabase, log_broker: LogBroker) -> None:
        self.db = db
        self.logger = logger
        self.log_broker = log_broker
        self.webui_dir: str | None = None

    async def start(self) -> None:
        core_lifecycle = AstrBotCoreLifecycle(self.log_broker, self.db)

        try:
            await core_lifecycle.initialize()
        except Exception as e:
            logger.critical(traceback.format_exc())
            logger.critical(f"😭 初始化 AstrBot 失败：{e} !!!")
            return

        core_task = core_lifecycle.start()

        webui_dir = self.webui_dir

        self.dashboard_server = AstrBotDashboard(
            core_lifecycle,
            self.db,
            core_lifecycle.dashboard_shutdown_event,
            webui_dir,
        )

        coro = self.dashboard_server.run()
        if coro:
            # 启动核心任务和仪表板服务器
            task = asyncio.gather(core_task, coro)
        else:
            task = core_task

        loop = asyncio.get_running_loop()
        running_task = asyncio.current_task()
        signal_handlers = []
        if running_task is not None and hasattr(signal, "SIGTERM"):
            try:
                loop.add_signal_handler(signal.SIGTERM, running_task.cancel)
                signal_handlers.append(signal.SIGTERM)
            except (NotImplementedError, RuntimeError):
                pass

        try:
            await task  # 整个AstrBot在这里运行
        except asyncio.CancelledError:
            logger.info("🌈 正在关闭 AstrBot...")
            # Close queued Loguru sinks before slower provider/plugin teardown so
            # launchd's termination timeout cannot leave OS semaphores behind.
            LogManager.shutdown()
            await core_lifecycle.stop()
        finally:
            for registered_signal in signal_handlers:
                loop.remove_signal_handler(registered_signal)
            LogManager.shutdown()
