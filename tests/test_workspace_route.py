from pathlib import Path

import pytest
from dc_engines.employee_insight_loop import TextSendResult
from dc_engines.pet_live.event_bus import publish_pet_event
from dc_engines.pet_live.identity import get_or_create_identity
from dc_engines.pet_live.store import PetLiveStore
from quart import Quart

from astrbot.dashboard.routes.route import RouteContext
from astrbot.dashboard.routes.workspace import WorkspaceReadResult, WorkspaceRoute


class _FakeWorkspaceSender:
    def __init__(self) -> None:
        self.calls = []

    async def send_text(
        self,
        receive_id: str,
        text: str,
        *,
        receive_id_type: str,
    ) -> TextSendResult:
        self.calls.append(
            {
                "receive_id": receive_id,
                "text": text,
                "receive_id_type": receive_id_type,
            }
        )
        return TextSendResult(
            success=True,
            provider_message_id="om_sent_1",
            raw={
                "receive_id": receive_id,
                "receive_id_type": receive_id_type,
                "chat_id": "oc_1",
            },
        )


class _FakeWorkspaceReader:
    def __init__(self, result: WorkspaceReadResult) -> None:
        self.result = result
        self.calls = []

    async def list_messages(
        self,
        *,
        chat_id: str,
        conversation_id: str,
        identity,
        limit: int,
    ) -> WorkspaceReadResult:
        self.calls.append(
            {
                "chat_id": chat_id,
                "conversation_id": conversation_id,
                "employee_id": identity.employee_id,
                "limit": limit,
            }
        )
        return self.result


@pytest.mark.asyncio
async def test_workspace_conversations_resolve_dc_feishu_session(
    tmp_path: Path,
) -> None:
    store = PetLiveStore(tmp_path / "data" / "feishu_pet.db")
    identity = get_or_create_identity(
        store,
        feishu_open_id="ou_user",
        employee_id="emp_001",
        desktop_session_id="sess_1",
    )
    app = Quart(__name__)
    WorkspaceRoute(RouteContext(config={}, app=app), dc_root=tmp_path)  # type: ignore[arg-type]

    async with app.test_client() as client:
        payload = await (
            await client.get(
                "/api/workspace/conversations",
                headers={"Cookie": "dc_feishu_session=sess_1"},
            )
        ).get_json()

    assert payload["status"] == "ok"
    assert payload["data"]["user"]["pet_id"] == identity.pet_id
    assert payload["data"]["user"]["employee_id"] == "emp_001"
    assert payload["data"]["conversations"] == [
        {
            "id": "ou_user",
            "title": "飞书小助手",
            "subtitle": "同一员工宠物身份",
            "last_message": "暂无桌面消息",
            "avatar": "助",
        }
    ]


@pytest.mark.asyncio
async def test_workspace_send_message_records_pet_light_feedback_event(
    tmp_path: Path,
) -> None:
    store = PetLiveStore(tmp_path / "data" / "feishu_pet.db")
    identity = get_or_create_identity(
        store,
        feishu_open_id="ou_user",
        employee_id="emp_001",
        desktop_session_id="sess_1",
    )
    app = Quart(__name__)
    WorkspaceRoute(RouteContext(config={}, app=app), dc_root=tmp_path)  # type: ignore[arg-type]

    async with app.test_client() as client:
        payload = await (
            await client.post(
                "/api/workspace/messages",
                headers={"Cookie": "dc_feishu_session=sess_1"},
                json={"conversation_id": "oc_1", "content": "hello"},
            )
        ).get_json()

    events = store.list_events_after(identity.pet_id, after_id=0)

    assert payload["status"] == "ok"
    assert payload["data"]["sent"] is False
    assert payload["data"]["event_id"] == events[0].id
    assert events[0].event_type == "feishu_message_received"
    assert events[0].user_id == "emp_001"
    assert events[0].source_ref.employee_id == "emp_001"
    assert events[0].payload["content"] == "hello"


@pytest.mark.asyncio
async def test_workspace_send_message_can_use_configured_feishu_sender(
    tmp_path: Path,
) -> None:
    store = PetLiveStore(tmp_path / "data" / "feishu_pet.db")
    identity = get_or_create_identity(
        store,
        feishu_open_id="ou_user",
        employee_id="emp_001",
        desktop_session_id="sess_1",
    )
    sender = _FakeWorkspaceSender()
    app = Quart(__name__)
    WorkspaceRoute(  # type: ignore[arg-type]
        RouteContext(config={}, app=app),
        dc_root=tmp_path,
        message_sender=sender,
    )

    async with app.test_client() as client:
        payload = await (
            await client.post(
                "/api/workspace/messages",
                headers={"Cookie": "dc_feishu_session=sess_1"},
                json={"conversation_id": "oc_1", "content": "hello"},
            )
        ).get_json()

    events = store.list_events_after(identity.pet_id, after_id=0)

    assert payload["status"] == "ok"
    assert payload["data"]["sent"] is True
    assert payload["data"]["provider_message_id"] == "om_sent_1"
    assert payload["data"]["delivered_event_id"] == events[1].id
    assert sender.calls == [
        {"receive_id": "oc_1", "text": "hello", "receive_id_type": "chat_id"}
    ]
    assert [event.event_type for event in events] == [
        "feishu_message_received",
        "assistant_response_sent",
    ]

    async with app.test_client() as client:
        messages_payload = await (
            await client.get(
                "/api/workspace/messages?conversation_id=oc_1",
                headers={"Cookie": "dc_feishu_session=sess_1"},
            )
        ).get_json()

    assert messages_payload["status"] == "ok"
    assert messages_payload["data"]["messages"] == [
        {
            "id": f"pet_event_{events[0].id}",
            "role": "user",
            "content": "hello",
            "created_at": events[0].created_at,
            "conversation_id": "oc_1",
            "event_id": events[0].id,
            "event_type": "feishu_message_received",
            "provider_message_id": "",
        },
        {
            "id": f"pet_event_{events[1].id}",
            "role": "assistant",
            "content": "已发送到飞书小助手",
            "created_at": events[1].created_at,
            "conversation_id": "oc_1",
            "event_id": events[1].id,
            "event_type": "assistant_response_sent",
            "provider_message_id": "om_sent_1",
        },
    ]


@pytest.mark.asyncio
async def test_workspace_conversations_use_outbox_preview(tmp_path: Path) -> None:
    store = PetLiveStore(tmp_path / "data" / "feishu_pet.db")
    get_or_create_identity(
        store,
        feishu_open_id="ou_user",
        employee_id="emp_001",
        desktop_session_id="sess_1",
    )
    sender = _FakeWorkspaceSender()
    app = Quart(__name__)
    WorkspaceRoute(  # type: ignore[arg-type]
        RouteContext(config={}, app=app),
        dc_root=tmp_path,
        message_sender=sender,
    )

    async with app.test_client() as client:
        await client.post(
            "/api/workspace/messages",
            headers={"Cookie": "dc_feishu_session=sess_1"},
            json={"conversation_id": "oc_1", "content": "hello"},
        )
        payload = await (
            await client.get(
                "/api/workspace/conversations",
                headers={"Cookie": "dc_feishu_session=sess_1"},
            )
        ).get_json()

    assert payload["status"] == "ok"
    assert payload["data"]["conversations"][0]["id"] == "oc_1"
    assert payload["data"]["conversations"][0]["last_message"] == "已发送到飞书小助手"


@pytest.mark.asyncio
async def test_workspace_messages_merge_feishu_history_and_outbox(
    tmp_path: Path,
) -> None:
    store = PetLiveStore(tmp_path / "data" / "feishu_pet.db")
    get_or_create_identity(
        store,
        feishu_open_id="ou_user",
        employee_id="emp_001",
        desktop_session_id="sess_1",
    )
    sender = _FakeWorkspaceSender()
    reader = _FakeWorkspaceReader(
        WorkspaceReadResult(
            success=True,
            messages=[
                {
                    "id": "om_sent_1",
                    "role": "assistant",
                    "content": "真实飞书历史",
                    "created_at": "2026-06-22T08:00:00+00:00",
                    "conversation_id": "ou_user",
                    "provider_message_id": "om_sent_1",
                }
            ],
        )
    )
    app = Quart(__name__)
    WorkspaceRoute(  # type: ignore[arg-type]
        RouteContext(config={}, app=app),
        dc_root=tmp_path,
        message_sender=sender,
        message_reader=reader,
    )

    async with app.test_client() as client:
        await client.post(
            "/api/workspace/messages",
            headers={"Cookie": "dc_feishu_session=sess_1"},
            json={"conversation_id": "ou_user", "content": "hello"},
        )
        payload = await (
            await client.get(
                "/api/workspace/messages?conversation_id=ou_user",
                headers={"Cookie": "dc_feishu_session=sess_1"},
            )
        ).get_json()

    messages = payload["data"]["messages"]
    events = store.list_events_after(
        store.get_identity_by_desktop_session_id("sess_1").pet_id,  # type: ignore[union-attr]
        after_id=0,
    )
    assert payload["data"]["history_status"] == "ok"
    assert reader.calls == [
        {
            "chat_id": "oc_1",
            "conversation_id": "ou_user",
            "employee_id": "emp_001",
            "limit": 50,
        }
    ]
    assert any(message["content"] == "hello" for message in messages)
    assert any(message["content"] == "真实飞书历史" for message in messages)
    assert [message.get("provider_message_id") for message in messages].count(
        "om_sent_1"
    ) == 1
    assert [event.event_type for event in events] == [
        "feishu_message_received",
        "assistant_response_sent",
    ]


@pytest.mark.asyncio
async def test_workspace_messages_record_new_assistant_history_once(
    tmp_path: Path,
) -> None:
    store = PetLiveStore(tmp_path / "data" / "feishu_pet.db")
    identity = get_or_create_identity(
        store,
        feishu_open_id="ou_user",
        employee_id="emp_001",
        desktop_session_id="sess_1",
    )
    publish_pet_event(
        store,
        pet_id=identity.pet_id,
        user_id=identity.employee_id,
        source="feishu_workspace",
        event_type="feishu_message_received",
        source_ref={
            "employee_id": identity.employee_id,
            "platform": "desktop_workspace",
            "conversation_id": "oc_1",
            "desktop_session_id": identity.desktop_session_id,
        },
        payload={"content": "hello", "direction": "desktop_to_assistant"},
        created_at="2026-06-22T07:59:59+00:00",
    )
    reader = _FakeWorkspaceReader(
        WorkspaceReadResult(
            success=True,
            messages=[
                    {
                        "id": "om_history_1",
                        "role": "assistant",
                        "content": "真实小助手回复",
                        "created_at": "2026-06-22T08:00:00+00:00",
                        "conversation_id": "oc_1",
                        "provider_message_id": "om_history_1",
                        "provider_chat_id": "oc_1",
                        "msg_type": "text",
                }
            ],
        )
    )
    app = Quart(__name__)
    WorkspaceRoute(  # type: ignore[arg-type]
        RouteContext(config={}, app=app),
        dc_root=tmp_path,
        message_reader=reader,
    )

    async with app.test_client() as client:
        for _ in range(2):
                payload = await (
                    await client.get(
                        "/api/workspace/messages?conversation_id=oc_1",
                        headers={"Cookie": "dc_feishu_session=sess_1"},
                    )
                ).get_json()

    events = store.list_events_after(identity.pet_id, after_id=0)

    assert payload["status"] == "ok"
    assert [event.event_type for event in events] == [
        "feishu_message_received",
        "assistant_message_observed",
    ]
    assert events[1].source_ref.message_id == "om_history_1"
    assert events[1].payload["content"] == "真实小助手回复"
    assert events[1].state_after is not None
    assert events[1].state_after.last_signal["summary"] == "真实小助手回复"


@pytest.mark.asyncio
async def test_workspace_messages_do_not_backfill_old_history_as_new_signal(
    tmp_path: Path,
) -> None:
    store = PetLiveStore(tmp_path / "data" / "feishu_pet.db")
    identity = get_or_create_identity(
        store,
        feishu_open_id="ou_user",
        employee_id="emp_001",
        desktop_session_id="sess_1",
    )
    publish_pet_event(
        store,
        pet_id=identity.pet_id,
        user_id=identity.employee_id,
        source="feishu_workspace",
        event_type="feishu_message_received",
        source_ref={
            "employee_id": identity.employee_id,
            "platform": "desktop_workspace",
            "conversation_id": "oc_1",
            "desktop_session_id": identity.desktop_session_id,
        },
        payload={"content": "hello", "direction": "desktop_to_assistant"},
        created_at="2026-06-22T08:00:00+00:00",
    )
    reader = _FakeWorkspaceReader(
        WorkspaceReadResult(
            success=True,
            messages=[
                {
                    "id": "om_old",
                    "role": "assistant",
                    "content": "旧历史",
                    "created_at": "2026-06-22T07:59:00+00:00",
                    "conversation_id": "oc_1",
                    "provider_message_id": "om_old",
                    "provider_chat_id": "oc_1",
                    "msg_type": "text",
                }
            ],
        )
    )
    app = Quart(__name__)
    WorkspaceRoute(  # type: ignore[arg-type]
        RouteContext(config={}, app=app),
        dc_root=tmp_path,
        message_reader=reader,
    )

    async with app.test_client() as client:
        payload = await (
            await client.get(
                "/api/workspace/messages?conversation_id=oc_1",
                headers={"Cookie": "dc_feishu_session=sess_1"},
            )
        ).get_json()

    events = store.list_events_after(identity.pet_id, after_id=0)

    assert payload["status"] == "ok"
    assert [event.event_type for event in events] == ["feishu_message_received"]


@pytest.mark.asyncio
async def test_workspace_messages_fallback_to_outbox_when_reader_fails(
    tmp_path: Path,
) -> None:
    store = PetLiveStore(tmp_path / "data" / "feishu_pet.db")
    get_or_create_identity(
        store,
        feishu_open_id="ou_user",
        employee_id="emp_001",
        desktop_session_id="sess_1",
    )
    sender = _FakeWorkspaceSender()
    reader = _FakeWorkspaceReader(
        WorkspaceReadResult(success=False, error="permission denied")
    )
    app = Quart(__name__)
    WorkspaceRoute(  # type: ignore[arg-type]
        RouteContext(config={}, app=app),
        dc_root=tmp_path,
        message_sender=sender,
        message_reader=reader,
    )

    async with app.test_client() as client:
        await client.post(
            "/api/workspace/messages",
            headers={"Cookie": "dc_feishu_session=sess_1"},
            json={"conversation_id": "ou_user", "content": "hello"},
        )
        payload = await (
            await client.get(
                "/api/workspace/messages?conversation_id=ou_user",
                headers={"Cookie": "dc_feishu_session=sess_1"},
            )
        ).get_json()

    assert payload["status"] == "ok"
    assert payload["data"]["history_status"] == "permission denied"
    assert [message["content"] for message in payload["data"]["messages"]] == [
        "hello",
        "已发送到飞书小助手",
    ]


@pytest.mark.asyncio
async def test_workspace_requires_bound_session(tmp_path: Path) -> None:
    app = Quart(__name__)
    WorkspaceRoute(RouteContext(config={}, app=app), dc_root=tmp_path)  # type: ignore[arg-type]

    async with app.test_client() as client:
        payload = await (
            await client.get(
                "/api/workspace/conversations",
                headers={"Cookie": "dc_feishu_session=missing"},
            )
        ).get_json()

    assert payload["status"] == "error"
    assert payload["message"] == "desktop session is not bound to a pet"
