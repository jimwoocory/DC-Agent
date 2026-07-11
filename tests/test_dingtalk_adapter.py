import asyncio
import threading

import pytest

from astrbot.core.platform.sources.dingtalk import dingtalk_adapter
from astrbot.core.platform.sources.dingtalk.dingtalk_adapter import (
    DINGTALK_RECONNECT_INITIAL_DELAY,
    DINGTALK_RECONNECT_MAX_DELAY,
    DingtalkPlatformAdapter,
    ManagedDingTalkStreamClient,
    _dingtalk_reconnect_delay,
)


def test_dingtalk_reconnect_delay_uses_exponential_backoff():
    assert [_dingtalk_reconnect_delay(i) for i in range(1, 5)] == [
        10,
        20,
        40,
        80,
    ]


def test_dingtalk_reconnect_delay_has_minimum_delay():
    assert _dingtalk_reconnect_delay(0) == DINGTALK_RECONNECT_INITIAL_DELAY
    assert _dingtalk_reconnect_delay(-1) == DINGTALK_RECONNECT_INITIAL_DELAY


def test_dingtalk_reconnect_delay_is_capped():
    assert _dingtalk_reconnect_delay(20) == DINGTALK_RECONNECT_MAX_DELAY


@pytest.mark.asyncio
async def test_managed_dingtalk_client_propagates_cancellation(monkeypatch):
    class FakeWebSocket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.sleep(3600)

    credential = type("Credential", (), {})()
    client = ManagedDingTalkStreamClient(credential)
    monkeypatch.setattr(
        client,
        "open_connection",
        lambda: {"endpoint": "wss://example.test", "ticket": "ticket"},
    )
    monkeypatch.setattr(
        dingtalk_adapter.websockets,
        "connect",
        lambda _: FakeWebSocket(),
    )

    task = asyncio.create_task(client.start())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert client.websocket is None


@pytest.mark.asyncio
async def test_dingtalk_reconnect_delay_wakes_on_terminate(monkeypatch):
    class ObservedEvent:
        def __init__(self) -> None:
            self._event = threading.Event()
            self.wait_started = threading.Event()
            self.wait_timeout: float | None = None

        def is_set(self) -> bool:
            return self._event.is_set()

        def set(self) -> None:
            self._event.set()

        def wait(self, timeout: float | None = None) -> bool:
            self.wait_timeout = timeout
            self.wait_started.set()
            return self._event.wait(timeout)

    class FailingClient:
        websocket = None

        async def start(self) -> None:
            raise RuntimeError("connect failed")

    terminated_event = ObservedEvent()
    adapter = DingtalkPlatformAdapter.__new__(DingtalkPlatformAdapter)
    adapter.client_ = FailingClient()
    adapter._shutdown_event = threading.Event()
    adapter._terminated_event = terminated_event

    monkeypatch.setattr(dingtalk_adapter, "_dingtalk_reconnect_delay", lambda _: 60)

    run_task = asyncio.create_task(adapter.run())
    try:
        wait_started = await asyncio.to_thread(terminated_event.wait_started.wait, 1)
        assert wait_started
        assert terminated_event.wait_timeout == 60

        await adapter.terminate()
        await asyncio.wait_for(run_task, timeout=1)
    finally:
        if not run_task.done():
            await adapter.terminate()
            run_task.cancel()
            await asyncio.gather(run_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_dingtalk_terminate_cancels_tracked_keepalive_tasks() -> None:
    adapter = DingtalkPlatformAdapter.__new__(DingtalkPlatformAdapter)
    adapter._terminated_event = threading.Event()
    adapter._shutdown_event = threading.Event()
    adapter.client_ = type("Client", (), {"websocket": None})()
    keepalive = asyncio.create_task(asyncio.sleep(3600))
    adapter._sdk_keepalive_tasks = {keepalive}

    await adapter.terminate()

    assert keepalive.done()
    assert keepalive.cancelled()
    assert adapter._sdk_keepalive_tasks == set()
