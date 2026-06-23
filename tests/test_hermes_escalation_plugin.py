from __future__ import annotations

from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_hermes_escalation_dispatch_prefers_context_dispatcher():
    from data.plugins.hermes_escalation_plugin.main import HermesEscalationPlugin

    calls = []

    async def dispatch(*args):
        calls.append(args)
        return True

    plugin = object.__new__(HermesEscalationPlugin)
    plugin.context = SimpleNamespace(dispatch_task_to_hermes=dispatch)
    task = SimpleNamespace(
        task_id="task-1",
        payload={"workflow_kind": "project_followup", "cognitive_context": {"x": "y"}},
    )
    event = SimpleNamespace(
        message_str="请深挖",
        unified_msg_origin="umo-1",
        get_platform_id=lambda: "lark",
        get_sender_id=lambda: "user-1",
    )

    ok = await plugin._dispatch_to_hermes(task, "project_followup", event)

    assert ok is True
    assert calls == [
        (
            "task-1",
            "project_followup",
            "请深挖",
            "umo-1",
            {"x": "y"},
        )
    ]


@pytest.mark.asyncio
async def test_hermes_escalation_fallback_uses_task_dispatcher(monkeypatch):
    from data.plugins.hermes_escalation_plugin.main import HermesEscalationPlugin

    calls = []

    class FakeDispatcher:
        def __init__(self, *, task_webhook_url: str, secret: str) -> None:
            self.task_webhook_url = task_webhook_url
            self.secret = secret

        async def dispatch(self, *args):
            calls.append(
                {
                    "url": self.task_webhook_url,
                    "secret": self.secret,
                    "args": args,
                }
            )
            return True

    monkeypatch.setattr(
        "data.plugins.hermes_escalation_plugin.main.HermesTaskDispatcher",
        FakeDispatcher,
    )

    plugin = object.__new__(HermesEscalationPlugin)
    plugin.context = SimpleNamespace(
        get_config=lambda: {
            "hermes_bridge": {
                "task_webhook_url": "http://hermes.local/task",
                "secret": "fallback-secret",
            }
        }
    )
    task = SimpleNamespace(
        task_id="task-2",
        payload={"cognitive_context": {"source": "fallback"}},
    )
    event = SimpleNamespace(
        message_str="继续深挖",
        unified_msg_origin="umo-2",
        get_platform_id=lambda: "lark",
        get_sender_id=lambda: "user-2",
    )

    ok = await plugin._dispatch_to_hermes(task, "project_followup", event)

    assert ok is True
    assert calls == [
        {
            "url": "http://hermes.local/task",
            "secret": "fallback-secret",
            "args": (
                "task-2",
                "project_followup",
                "继续深挖",
                "umo-2",
                {"source": "fallback"},
            ),
        }
    ]
