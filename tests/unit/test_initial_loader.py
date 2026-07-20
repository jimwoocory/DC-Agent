import asyncio
import signal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from astrbot.core.initial_loader import InitialLoader


@pytest.mark.asyncio
async def test_sigterm_cancels_runtime_and_closes_log_sinks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle = MagicMock()
    lifecycle.initialize = AsyncMock()
    lifecycle.stop = AsyncMock()
    lifecycle.dashboard_shutdown_event = asyncio.Event()

    async def wait_forever() -> None:
        await asyncio.Event().wait()

    lifecycle.start.side_effect = wait_forever
    dashboard = MagicMock()
    dashboard.run.side_effect = wait_forever

    loop = asyncio.get_running_loop()
    handlers = {}
    removed = []
    monkeypatch.setattr(
        loop,
        "add_signal_handler",
        lambda registered_signal, callback: handlers.setdefault(
            registered_signal, callback
        ),
    )
    monkeypatch.setattr(
        loop,
        "remove_signal_handler",
        lambda registered_signal: removed.append(registered_signal) or True,
    )

    with (
        patch(
            "astrbot.core.initial_loader.AstrBotCoreLifecycle",
            return_value=lifecycle,
        ),
        patch("astrbot.core.initial_loader.AstrBotDashboard", return_value=dashboard),
        patch("astrbot.core.initial_loader.LogManager.shutdown") as shutdown,
    ):
        loader = InitialLoader(MagicMock(), MagicMock())
        run_task = asyncio.create_task(loader.start())
        await asyncio.sleep(0)

        handlers[signal.SIGTERM]()
        await run_task

    lifecycle.stop.assert_awaited_once()
    assert shutdown.call_count == 2
    assert removed == [signal.SIGTERM]
