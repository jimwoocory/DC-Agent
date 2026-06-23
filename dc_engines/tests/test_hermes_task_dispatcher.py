from __future__ import annotations

import hashlib
import hmac
import json

import pytest


@pytest.mark.asyncio
async def test_hermes_task_dispatcher_signs_task_webhook(monkeypatch):
    from dc_engines.hermes_bridge_engine.task_dispatcher import HermesTaskDispatcher

    captured: dict[str, object] = {}

    class FakeResponse:
        status = 202

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def text(self) -> str:
            return "ok"

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def post(self, url: str, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            return FakeResponse()

    monkeypatch.setattr(
        "dc_engines.hermes_bridge_engine.task_dispatcher.aiohttp.ClientSession",
        FakeSession,
    )
    dispatcher = HermesTaskDispatcher(
        task_webhook_url="http://hermes.local/webhooks/astrbot_task",
        secret="test-secret",
    )

    ok = await dispatcher.dispatch(
        "task_001",
        "content_sop_workflow",
        "生成客户邀约文案",
        "lark:tenant:user",
        {"trace": "beta"},
    )

    body = captured["data"]
    assert isinstance(body, bytes)
    expected_sig = hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()
    assert ok is True
    assert captured["url"] == "http://hermes.local/webhooks/astrbot_task"
    assert "json" not in captured
    assert json.loads(body.decode("utf-8"))["task_id"] == "task_001"
    headers = captured["headers"]
    assert headers["X-Hub-Signature-256"] == f"sha256={expected_sig}"
    assert headers["X-Webhook-Event"] == "harness_task"


@pytest.mark.asyncio
async def test_hermes_task_dispatcher_includes_extra_payload(monkeypatch):
    from dc_engines.hermes_bridge_engine.task_dispatcher import HermesTaskDispatcher

    captured: dict[str, object] = {}

    class FakeResponse:
        status = 202

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def text(self) -> str:
            return "ok"

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def post(self, url: str, **kwargs):
            captured.update(kwargs)
            return FakeResponse()

    monkeypatch.setattr(
        "dc_engines.hermes_bridge_engine.task_dispatcher.aiohttp.ClientSession",
        FakeSession,
    )
    dispatcher = HermesTaskDispatcher(
        task_webhook_url="http://hermes.local/webhooks/astrbot_task",
        secret="test-secret",
    )

    ok = await dispatcher.dispatch(
        "pf_001",
        "persona_factory",
        "Grace Hopper",
        "lark:tenant:user",
        {"persona_factory_request_id": "pf_001"},
        extra_payload={
            "engine": "dc_engines.persona_factory",
            "request": {"request_id": "pf_001"},
            "plan": {"source_manifest_path": "/tmp/source_manifest.json"},
        },
    )

    body = json.loads(captured["data"].decode("utf-8"))
    assert ok is True
    assert body["workflow_kind"] == "persona_factory"
    assert body["engine"] == "dc_engines.persona_factory"
    assert body["request"]["request_id"] == "pf_001"
    assert body["plan"]["source_manifest_path"] == "/tmp/source_manifest.json"
