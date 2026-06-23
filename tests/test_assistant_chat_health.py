from __future__ import annotations

import importlib.util
import json
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from quart import Quart

from astrbot.core.assistant_chat_health import assistant_chat_health_tracker
from astrbot.core.event_bus import EventBus
from astrbot.core.platform.sources.webchat.webchat_queue_mgr import webchat_queue_mgr
from astrbot.dashboard.routes import chat as chat_module
from astrbot.dashboard.routes.chat import ChatRoute
from astrbot.dashboard.routes.route import RouteContext
from harness.evaluator.kb_import_contract import load_contract, validate_contract

CONTRACT = Path("harness/contracts/assistant_chat_health.json")
SERVER = Path("astrbot/dashboard/server.py")
WATCHDOG_ENGINE = Path("scripts-watchdog/watchdog_engine.py")
WATCHDOG_SCRIPT = Path("scripts-watchdog/dc-watchdog.sh")
DASHBOARD_ROUTE = Path("dashboard/src/router/MainRoutes.ts")
DASHBOARD_SIDEBAR = Path("dashboard/src/layouts/full/vertical-sidebar/sidebarItem.ts")
DASHBOARD_PAGE = Path("dashboard/src/views/AssistantHealthPage.vue")
ZH_NAVIGATION = Path("dashboard/src/i18n/locales/zh-CN/core/navigation.json")
STATIC_ROUTE = Path("astrbot/dashboard/routes/static_file.py")


class FakeDB:
    async def get_platform_sessions_by_creator_paginated(self, **_kwargs):
        return [], 0


class FakeProviderStatResult:
    def __init__(self, records: list[SimpleNamespace]) -> None:
        self.records = records

    def scalars(self):
        return self

    def all(self):
        return self.records


class FakeProviderStatSession:
    def __init__(self, records: list[SimpleNamespace]) -> None:
        self.records = records

    async def execute(self, _query):
        return FakeProviderStatResult(self.records)


class FakeProviderStatDB(FakeDB):
    def __init__(self, records: list[SimpleNamespace]) -> None:
        self.records = records

    @asynccontextmanager
    async def get_db(self):
        yield FakeProviderStatSession(self.records)


class BrokenDB(FakeDB):
    async def get_platform_sessions_by_creator_paginated(self, **_kwargs):
        raise RuntimeError("secret database detail")


def _clear_webchat_queues() -> None:
    for conversation_id in list(webchat_queue_mgr.queues):
        webchat_queue_mgr.remove_queues(conversation_id)
    for request_id in list(webchat_queue_mgr.back_queues):
        webchat_queue_mgr.remove_back_queue(request_id)
    assistant_chat_health_tracker.clear()


class FakeScheduler:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    async def execute(self, _event) -> None:
        if self.fail:
            raise RuntimeError("platform pipeline failed")


class FakePlatformEvent:
    unified_msg_origin = "lark:FriendMessage:lark!secret-user!secret-chat"

    def __init__(self, platform_name: str = "lark") -> None:
        self.platform_name = platform_name

    def get_platform_name(self) -> str:
        return self.platform_name


def _build_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    db: FakeDB | None = None,
) -> tuple[Quart, ChatRoute]:
    data_dir = tmp_path / "data"
    monkeypatch.setattr(chat_module, "get_astrbot_data_path", lambda: str(data_dir))
    app = Quart(__name__)
    lifecycle = SimpleNamespace(
        conversation_manager=SimpleNamespace(session_conversations={}),
        platform_message_history_manager=SimpleNamespace(),
        umop_config_router=SimpleNamespace(),
    )
    route = ChatRoute(
        RouteContext(config={}, app=app),  # type: ignore[arg-type]
        db or FakeDB(),  # type: ignore[arg-type]
        lifecycle,  # type: ignore[arg-type]
    )
    return app, route


def _load_watchdog_engine():
    spec = importlib.util.spec_from_file_location("watchdog_engine", WATCHDOG_ENGINE)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_assistant_chat_health_contract_is_valid() -> None:
    contract = load_contract(CONTRACT)

    assert validate_contract(contract) == []


@pytest.mark.asyncio
async def test_assistant_chat_health_returns_metadata_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_webchat_queues()
    app, _route = _build_route(tmp_path, monkeypatch)

    async with app.test_client() as client:
        response = await client.get("/api/chat/health")
        payload = await response.get_json()

    assert response.status_code == 200
    assert payload["status"] == "ok"
    assert payload["data"]["runtime"]["active_runs"] == 0
    assert payload["data"]["activity"]["started_15m"] == 0
    assert payload["data"]["collection"]["total_calls"] == 0
    assert payload["data"]["queues"]["queues"] == 0
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "message_parts" not in serialized
    assert "selected_text" not in serialized


@pytest.mark.asyncio
async def test_assistant_chat_health_counts_platform_pipeline_runs_without_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_webchat_queues()
    app, _route = _build_route(tmp_path, monkeypatch)
    bus = EventBus(  # type: ignore[arg-type]
        event_queue=None,
        pipeline_scheduler_mapping={},
        astrbot_config_mgr=None,
    )

    await bus._execute_with_health(FakeScheduler(), FakePlatformEvent())

    async with app.test_client() as client:
        response = await client.get("/api/chat/health")
        payload = await response.get_json()

    assert response.status_code == 200
    activity = payload["data"]["activity"]
    assert activity["started_5m"] == 1
    assert activity["started_15m"] == 1
    assert activity["completed_15m"] == 1
    assert activity["last_kind"] == "platform"
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "secret-user" not in serialized
    assert "secret-chat" not in serialized


@pytest.mark.asyncio
async def test_assistant_chat_health_collects_provider_stats_since_today_midnight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_webchat_queues()
    records = [
        SimpleNamespace(created_at=datetime.now(timezone.utc), status="completed"),
        SimpleNamespace(created_at=datetime.now(timezone.utc), status="error"),
        SimpleNamespace(created_at=datetime.now(timezone.utc), status="aborted"),
    ]
    app, _route = _build_route(
        tmp_path,
        monkeypatch,
        FakeProviderStatDB(records),
    )

    async with app.test_client() as client:
        response = await client.get("/api/chat/health")
        payload = await response.get_json()

    assert response.status_code == 200
    collection = payload["data"]["collection"]
    assert collection["source"] == "provider_stats"
    assert "T00:00:00" in collection["since_local"]
    assert collection["total_calls"] == 3
    assert collection["completed_calls"] == 1
    assert collection["failed_calls"] == 1
    assert collection["aborted_calls"] == 1
    assert collection["last_call_at"]
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "secret-user" not in serialized
    assert "message_parts" not in serialized


@pytest.mark.asyncio
async def test_assistant_chat_health_does_not_double_count_webchat_event_bus(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_webchat_queues()
    app, _route = _build_route(tmp_path, monkeypatch)
    bus = EventBus(  # type: ignore[arg-type]
        event_queue=None,
        pipeline_scheduler_mapping={},
        astrbot_config_mgr=None,
    )

    await bus._execute_with_health(FakeScheduler(), FakePlatformEvent("webchat"))

    async with app.test_client() as client:
        response = await client.get("/api/chat/health")
        payload = await response.get_json()

    assert response.status_code == 200
    assert payload["data"]["activity"]["started_15m"] == 0


@pytest.mark.asyncio
async def test_assistant_chat_health_reports_recent_activity_without_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_webchat_queues()
    app, route = _build_route(tmp_path, monkeypatch)
    secret_session_id = "session-secret-id"
    started_at = time.time() - 40
    route._record_chat_activity(
        kind="session",
        phase="started",
        at=started_at,
    )
    route._record_chat_activity(
        kind="session",
        phase="completed",
        at=time.time() - 5,
        started_at=started_at,
    )
    route.running_convs[secret_session_id] = {
        "active_count": 1,
        "kind": "session",
        "started_at": time.time() - 2,
        "last_seen_at": time.time() - 2,
    }

    async with app.test_client() as client:
        response = await client.get("/api/chat/health")
        payload = await response.get_json()

    assert response.status_code == 200
    activity = payload["data"]["activity"]
    assert activity["started_5m"] == 1
    assert activity["started_15m"] == 1
    assert activity["started_60m"] == 1
    assert activity["completed_15m"] == 1
    assert activity["last_event"] == "completed"
    assert activity["last_kind"] == "session"
    assert activity["last_duration_sec"] >= 30
    serialized = json.dumps(payload, ensure_ascii=False)
    assert secret_session_id not in serialized
    assert "message_parts" not in serialized
    assert "selected_text" not in serialized


@pytest.mark.asyncio
async def test_assistant_chat_health_reports_stale_threads_without_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_webchat_queues()
    app, route = _build_route(tmp_path, monkeypatch)
    secret_thread_id = "thread-secret-id"
    route.running_convs[secret_thread_id] = {
        "active_count": 1,
        "kind": "thread",
        "started_at": time.time() - 120,
        "last_seen_at": time.time() - 120,
    }

    async with app.test_client() as client:
        response = await client.get("/api/chat/health?stale_after_sec=60")
        payload = await response.get_json()

    assert response.status_code == 503
    assert payload["status"] == "error"
    assert payload["data"]["runtime"]["active_threads"] == 1
    assert payload["data"]["runtime"]["stale_runs"] == 1
    assert secret_thread_id not in json.dumps(payload, ensure_ascii=False)


@pytest.mark.asyncio
async def test_assistant_chat_health_masks_storage_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_webchat_queues()
    app, _route = _build_route(tmp_path, monkeypatch, BrokenDB())

    async with app.test_client() as client:
        response = await client.get("/api/chat/health")
        payload = await response.get_json()

    assert response.status_code == 503
    assert payload["data"]["checks"][2] == {
        "name": "chat_storage",
        "status": "fail",
        "reason": "storage_unavailable",
    }
    assert "secret database detail" not in json.dumps(payload, ensure_ascii=False)


def test_assistant_chat_health_is_watchdog_visible_without_dashboard_jwt() -> None:
    assert '"/api/chat/health"' in SERVER.read_text(encoding="utf-8")


def test_watchdog_uses_strict_assistant_chat_health_probe() -> None:
    engine = _load_watchdog_engine()
    probes = {probe.name: probe for probe in engine.ACTIVE_PROBES}

    probe = probes["assistant_chat_health"]
    assert probe.kind == "http_strict"
    assert probe.target == "http://127.0.0.1:6185/api/chat/health"
    assert "http_strict) cur=$(probe_http_strict" in WATCHDOG_SCRIPT.read_text(
        encoding="utf-8"
    )


def test_assistant_chat_health_dashboard_surface_is_registered() -> None:
    route_source = DASHBOARD_ROUTE.read_text(encoding="utf-8")
    sidebar_source = DASHBOARD_SIDEBAR.read_text(encoding="utf-8")
    page_source = DASHBOARD_PAGE.read_text(encoding="utf-8")
    zh_navigation = ZH_NAVIGATION.read_text(encoding="utf-8")
    static_source = STATIC_ROUTE.read_text(encoding="utf-8")

    assert "path: '/assistant-health'" in route_source
    assert "AssistantHealthPage.vue" in route_source
    assert "core.navigation.assistantHealth" in sidebar_source
    assert "to: '/assistant-health'" in sidebar_source
    assert '"assistantHealth": "小助手健康"' in zh_navigation
    assert '"/assistant-health"' in static_source
    assert "/api/chat/health" in page_source
