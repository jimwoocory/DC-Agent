import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import lark_oapi as lark
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTriggerResponse,
)
from lark_oapi.ws import client as lark_ws_client
from lark_oapi.ws.const import (
    HEADER_MESSAGE_ID,
    HEADER_SEQ,
    HEADER_SUM,
    HEADER_TRACE_ID,
    HEADER_TYPE,
)
from lark_oapi.ws.enum import FrameType
from lark_oapi.ws.enum import MessageType as LarkWSMessageType
from lark_oapi.ws.pb.pbbp2_pb2 import Frame
from websockets.exceptions import ConnectionClosedOK
from websockets.frames import Close

import astrbot.api.message_components as Comp
from astrbot.core.platform.astrbot_message import (
    AstrBotMessage,
    MessageMember,
    MessageType,
)
from astrbot.core.platform.sources.lark.lark_adapter import (
    LarkPlatformAdapter,
    _LarkCardCallbackClient,
)


def test_lark_card_action_callback_acknowledges_before_async_processing() -> None:
    async def run() -> None:
        adapter = LarkPlatformAdapter(
            {
                "id": "lark-card-action-test",
                "app_id": "cli_test",
                "app_secret": "secret",
                "lark_connection_mode": "socket",
            },
            {},
            asyncio.Queue(),
        )
        adapter.convert_card_action = AsyncMock()
        callback = adapter.event_handler._callback_processor_map[
            "p2.card.action.trigger"
        ]
        event = SimpleNamespace(event=None)

        response = callback.do(event)
        await asyncio.sleep(0)

        assert isinstance(response, P2CardActionTriggerResponse)
        adapter.convert_card_action.assert_awaited_once_with(event)

    asyncio.run(run())


def test_lark_socket_dispatches_and_acknowledges_card_frames() -> None:
    async def run() -> None:
        adapter = LarkPlatformAdapter(
            {
                "id": "lark-card-frame-test",
                "app_id": "cli_test",
                "app_secret": "secret",
                "lark_connection_mode": "socket",
            },
            {},
            asyncio.Queue(),
        )
        adapter.convert_card_action = AsyncMock()
        adapter.client._write_message = AsyncMock()

        frame = Frame()
        frame.method = FrameType.DATA.value
        frame.SeqID = 1
        frame.LogID = 1
        frame.service = 1
        for key, value in (
            (HEADER_MESSAGE_ID, "message-card-1"),
            (HEADER_TRACE_ID, "trace-card-1"),
            (HEADER_SUM, "1"),
            (HEADER_SEQ, "0"),
            (HEADER_TYPE, LarkWSMessageType.CARD.value),
        ):
            header = frame.headers.add()
            header.key = key
            header.value = value
        frame.payload = json.dumps(
            {
                "schema": "2.0",
                "event_type": "card.action.trigger",
                "operator": {"open_id": "ou_user"},
                "action": {"tag": "button", "value": {"action": "show_task"}},
                "context": {
                    "open_message_id": "om_card",
                    "open_chat_id": "oc_chat",
                },
            }
        ).encode()

        await adapter.client._handle_data_frame(frame)
        await asyncio.sleep(0)

        adapter.client._write_message.assert_awaited_once()
        adapter.convert_card_action.assert_awaited_once()

    asyncio.run(run())


def test_lark_card_action_records_minimal_trace_metadata() -> None:
    async def run() -> None:
        adapter = LarkPlatformAdapter.__new__(LarkPlatformAdapter)
        adapter.bot_name = "小助手"
        adapter.handle_msg = AsyncMock()
        data = SimpleNamespace(
            operator=SimpleNamespace(open_id="ou_private_operator", user_id=None),
            action=SimpleNamespace(
                value={
                    "source": "daily_response",
                    "action": "regenerate",
                    "task_id": "task-001",
                    "prompt": "must not enter audit metadata",
                },
                form_value={"full_chat": "must not enter audit metadata"},
                input_value="must not enter audit metadata",
                tag="button",
                name="regenerate",
            ),
            context=SimpleNamespace(
                open_chat_id="oc_trace",
                open_message_id="om_trace",
            ),
            token="callback-secret",
        )

        with patch(
            "dc_engines.card_runtime.record_card_action_via_runtime"
        ) as record_action:
            await adapter.convert_card_action(SimpleNamespace(event=data))

        record_action.assert_called_once_with(
            message_id="om_trace",
            conversation_id="oc_trace",
            action="regenerate",
            source="daily_response",
            task_id="task-001",
            operator_id="ou_private_operator",
        )
        adapter.handle_msg.assert_awaited_once()

    asyncio.run(run())


def test_lark_socket_connect_uses_the_running_application_loop() -> None:
    async def run() -> None:
        stale_loop = asyncio.new_event_loop()
        previous_loop = lark_ws_client.loop
        lark_ws_client.loop = stale_loop
        try:
            adapter = LarkPlatformAdapter(
                {
                    "id": "lark-loop-test",
                    "app_id": "cli_test",
                    "app_secret": "secret",
                    "lark_connection_mode": "socket",
                },
                {},
                asyncio.Queue(),
            )
            with patch.object(lark.ws.Client, "_connect", new=AsyncMock()) as connect:
                await adapter.client._connect()

            connect.assert_awaited_once()
            assert lark_ws_client.loop is asyncio.get_running_loop()
        finally:
            lark_ws_client.loop = previous_loop
            stale_loop.close()

    asyncio.run(run())


def test_lark_socket_clean_close_is_not_reported_as_an_error() -> None:
    async def run() -> None:
        client = _LarkCardCallbackClient.__new__(_LarkCardCallbackClient)
        client._conn = SimpleNamespace(
            recv=AsyncMock(
                side_effect=ConnectionClosedOK(
                    Close(1000, "bye"),
                    Close(1000, "bye"),
                    True,
                )
            )
        )
        client._auto_reconnect = False
        client._disconnect = AsyncMock()
        client._reconnect = AsyncMock()

        with patch(
            "astrbot.core.platform.sources.lark.lark_adapter.logger.error"
        ) as log_error:
            await client._receive_message_loop()

        client._disconnect.assert_awaited_once()
        client._reconnect.assert_not_awaited()
        log_error.assert_not_called()

    asyncio.run(run())


def test_lark_polling_builds_private_message_from_list_item() -> None:
    adapter = LarkPlatformAdapter.__new__(LarkPlatformAdapter)
    adapter.appid = "cli_bot"
    adapter.bot_name = "小助手"

    item = SimpleNamespace(
        message_id="om_polled",
        msg_type="text",
        chat_type="p2p",
        chat_id="oc_chat",
        create_time="1781517600000",
        sender=SimpleNamespace(id="ou_user"),
        body=SimpleNamespace(content=json.dumps({"text": "你好，小助手"})),
        mentions=None,
    )

    message = asyncio.run(adapter._build_polled_message(item))

    assert message is not None
    assert message.message_id == "om_polled"
    assert message.type == MessageType.FRIEND_MESSAGE
    assert message.sender.user_id == "ou_user"
    assert message.session_id == "ou_user"
    assert message.message_str == "你好，小助手"


def _make_adapter_for_buffering() -> LarkPlatformAdapter:
    adapter = LarkPlatformAdapter.__new__(LarkPlatformAdapter)
    adapter.config = {"id": "lark-test"}
    adapter._event_queue = asyncio.Queue()
    adapter.lark_api = MagicMock()
    adapter._last_event_at = None
    adapter.multimodal_merge_window_seconds = 0.01
    adapter.file_multimodal_merge_window_seconds = 0.01
    adapter._pending_multimodal_messages = {}
    return adapter


def _make_abm(
    *,
    message_id: str,
    message: list[Comp.BaseMessageComponent],
    message_str: str,
) -> AstrBotMessage:
    abm = AstrBotMessage()
    abm.message_id = message_id
    abm.message = message
    abm.message_str = message_str
    abm.type = MessageType.FRIEND_MESSAGE
    abm.self_id = "bot"
    abm.sender = MessageMember(user_id="ou_user", nickname="ou_user")
    abm.session_id = "ou_user"
    abm.raw_message = SimpleNamespace(message_id=message_id)
    return abm


def test_lark_image_only_message_waits_for_adjacent_fragments() -> None:
    async def run() -> None:
        adapter = _make_adapter_for_buffering()
        image_msg = _make_abm(
            message_id="om_image",
            message=[Comp.Image.fromBase64("aW1hZ2U=")],
            message_str="[image]",
        )

        await adapter.handle_msg(image_msg)

        assert adapter._event_queue.empty()
        await asyncio.sleep(0.03)
        event = adapter._event_queue.get_nowait()
        assert event.message_str == "[image]"
        assert event.message_obj.message_id == "om_image"

    asyncio.run(run())


def test_lark_plain_text_and_commands_are_queued_immediately() -> None:
    async def run() -> None:
        adapter = _make_adapter_for_buffering()
        plain = _make_abm(
            message_id="om_plain",
            message=[Comp.Plain("帮我搭建一个活动方案框架")],
            message_str="帮我搭建一个活动方案框架",
        )
        command = _make_abm(
            message_id="om_stop",
            message=[Comp.Plain("/stop")],
            message_str="/stop",
        )

        await adapter.handle_msg(plain)
        await adapter.handle_msg(command)

        first = adapter._event_queue.get_nowait()
        second = adapter._event_queue.get_nowait()
        assert first.message_obj.message_id == "om_plain"
        assert second.message_obj.message_id == "om_stop"
        assert adapter._pending_multimodal_messages == {}

    asyncio.run(run())


def test_lark_media_menu_labels_are_queued_immediately() -> None:
    async def run() -> None:
        adapter = _make_adapter_for_buffering()
        image_menu = _make_abm(
            message_id="om_image_menu",
            message=[Comp.Plain("生成图片")],
            message_str="生成图片",
        )
        file_menu = _make_abm(
            message_id="om_file_menu",
            message=[Comp.Plain("处理文件")],
            message_str="处理文件",
        )

        await adapter.handle_msg(image_menu)
        await adapter.handle_msg(file_menu)

        first = adapter._event_queue.get_nowait()
        second = adapter._event_queue.get_nowait()
        assert first.message_obj.message_id == "om_image_menu"
        assert second.message_obj.message_id == "om_file_menu"
        assert adapter._pending_multimodal_messages == {}

    asyncio.run(run())


def test_lark_merges_image_then_follow_up_text() -> None:
    async def run() -> None:
        adapter = _make_adapter_for_buffering()
        image_msg = _make_abm(
            message_id="om_image",
            message=[Comp.Image.fromBase64("aW1hZ2U=")],
            message_str="[image]",
        )
        text_msg = _make_abm(
            message_id="om_text",
            message=[Comp.Plain("这张图片的人物帮我抠出来")],
            message_str="这张图片的人物帮我抠出来",
        )

        await adapter.handle_msg(image_msg)
        await adapter.handle_msg(text_msg)

        event = adapter._event_queue.get_nowait()
        assert event.message_obj.message_id == "om_text"
        assert event.message_str == "[image] 这张图片的人物帮我抠出来"
        assert [type(comp) for comp in event.message_obj.message] == [
            Comp.Image,
            Comp.Plain,
        ]
        assert adapter._event_queue.empty()

    asyncio.run(run())


def test_lark_merges_text_then_follow_up_image() -> None:
    async def run() -> None:
        adapter = _make_adapter_for_buffering()
        text_msg = _make_abm(
            message_id="om_text",
            message=[Comp.Plain("这张图片的人物帮我抠出来")],
            message_str="这张图片的人物帮我抠出来",
        )
        image_msg = _make_abm(
            message_id="om_image",
            message=[Comp.Image.fromBase64("aW1hZ2U=")],
            message_str="[image]",
        )

        await adapter.handle_msg(text_msg)
        await adapter.handle_msg(image_msg)

        event = adapter._event_queue.get_nowait()
        assert event.message_obj.message_id == "om_image"
        assert event.message_str == "这张图片的人物帮我抠出来 [image]"
        assert [type(comp) for comp in event.message_obj.message] == [
            Comp.Plain,
            Comp.Image,
        ]
        assert adapter._event_queue.empty()

    asyncio.run(run())


def test_lark_merges_file_then_follow_up_text() -> None:
    async def run() -> None:
        adapter = _make_adapter_for_buffering()
        file_msg = _make_abm(
            message_id="om_file",
            message=[Comp.File(name="方案.pdf", file="/tmp/plan.pdf")],
            message_str="方案.pdf",
        )
        text_msg = _make_abm(
            message_id="om_text",
            message=[Comp.Plain("帮我总结这份方案")],
            message_str="帮我总结这份方案",
        )

        await adapter.handle_msg(file_msg)
        await adapter.handle_msg(text_msg)

        event = adapter._event_queue.get_nowait()
        assert event.message_obj.message_id == "om_text"
        assert event.message_str == "方案.pdf 帮我总结这份方案"
        assert [type(comp) for comp in event.message_obj.message] == [
            Comp.File,
            Comp.Plain,
        ]
        assert adapter._event_queue.empty()

    asyncio.run(run())


def test_lark_merges_text_then_follow_up_file() -> None:
    async def run() -> None:
        adapter = _make_adapter_for_buffering()
        text_msg = _make_abm(
            message_id="om_text",
            message=[Comp.Plain("帮我总结这份方案")],
            message_str="帮我总结这份方案",
        )
        file_msg = _make_abm(
            message_id="om_file",
            message=[Comp.File(name="方案.pdf", file="/tmp/plan.pdf")],
            message_str="方案.pdf",
        )

        await adapter.handle_msg(text_msg)
        await adapter.handle_msg(file_msg)

        event = adapter._event_queue.get_nowait()
        assert event.message_obj.message_id == "om_file"
        assert event.message_str == "帮我总结这份方案 方案.pdf"
        assert [type(comp) for comp in event.message_obj.message] == [
            Comp.Plain,
            Comp.File,
        ]
        assert adapter._event_queue.empty()

    asyncio.run(run())


def test_lark_merges_batch_files_and_follow_up_text() -> None:
    async def run() -> None:
        adapter = _make_adapter_for_buffering()
        first_file = _make_abm(
            message_id="om_file_1",
            message=[Comp.File(name="方案A.pdf", file="/tmp/a.pdf")],
            message_str="方案A.pdf",
        )
        second_file = _make_abm(
            message_id="om_file_2",
            message=[Comp.File(name="方案B.pdf", file="/tmp/b.pdf")],
            message_str="方案B.pdf",
        )
        text_msg = _make_abm(
            message_id="om_text",
            message=[Comp.Plain("这两份都帮我提炼重点")],
            message_str="这两份都帮我提炼重点",
        )

        await adapter.handle_msg(first_file)
        await adapter.handle_msg(second_file)
        await adapter.handle_msg(text_msg)

        event = adapter._event_queue.get_nowait()
        assert event.message_obj.message_id == "om_text"
        assert event.message_str == "方案A.pdf 方案B.pdf 这两份都帮我提炼重点"
        assert [type(comp) for comp in event.message_obj.message] == [
            Comp.File,
            Comp.File,
            Comp.Plain,
        ]
        assert adapter._event_queue.empty()

    asyncio.run(run())


def test_lark_file_fragments_can_use_longer_merge_window() -> None:
    adapter = _make_adapter_for_buffering()
    adapter.multimodal_merge_window_seconds = 5.0
    adapter.file_multimodal_merge_window_seconds = 60.0
    file_msg = _make_abm(
        message_id="om_file",
        message=[Comp.File(name="方案.pdf", file="/tmp/plan.pdf")],
        message_str="方案.pdf",
    )
    image_msg = _make_abm(
        message_id="om_image",
        message=[Comp.Image.fromBase64("aW1hZ2U=")],
        message_str="[image]",
    )

    assert adapter._merge_window_for_fragments(file_msg) == 60.0
    assert adapter._merge_window_for_fragments(image_msg) == 5.0


def test_lark_does_not_merge_different_senders_in_same_group() -> None:
    async def run() -> None:
        adapter = _make_adapter_for_buffering()
        first = _make_abm(
            message_id="om_user_1",
            message=[Comp.Plain("第一位同事的消息")],
            message_str="第一位同事的消息",
        )
        first.type = MessageType.GROUP_MESSAGE
        first.session_id = "oc_group"
        first.sender = MessageMember(user_id="ou_user_1", nickname="user1")
        second = _make_abm(
            message_id="om_user_2",
            message=[Comp.Image.fromBase64("aW1hZ2U=")],
            message_str="[image]",
        )
        second.type = MessageType.GROUP_MESSAGE
        second.session_id = "oc_group"
        second.sender = MessageMember(user_id="ou_user_2", nickname="user2")

        await adapter.handle_msg(first)
        await adapter.handle_msg(second)

        await asyncio.sleep(0.03)
        events = [adapter._event_queue.get_nowait(), adapter._event_queue.get_nowait()]
        assert [event.message_obj.message_id for event in events] == [
            "om_user_1",
            "om_user_2",
        ]
        assert [event.message_str for event in events] == [
            "第一位同事的消息",
            "[image]",
        ]

    asyncio.run(run())


def test_lark_polling_learns_new_chat_ids_when_enabled(tmp_path) -> None:
    adapter = LarkPlatformAdapter.__new__(LarkPlatformAdapter)
    adapter.polling_fallback_enabled = True
    adapter.polling_fallback_chat_ids = ["oc_existing"]
    adapter.polling_chat_store_path = tmp_path / "learned_chats.json"
    adapter.appid = "cli_bot"

    assert adapter._remember_polling_chat_id("oc_new", reason="test") is True
    assert adapter._remember_polling_chat_id("oc_new", reason="test") is False

    assert adapter.polling_fallback_chat_ids == ["oc_existing", "oc_new"]
    assert json.loads(adapter.polling_chat_store_path.read_text())["chat_ids"] == [
        "oc_existing",
        "oc_new",
    ]


def test_lark_polling_learned_chat_ids_survive_adapter_restart(tmp_path) -> None:
    first = LarkPlatformAdapter.__new__(LarkPlatformAdapter)
    first.polling_fallback_enabled = True
    first.polling_fallback_chat_ids = ["oc_configured"]
    first.polling_chat_store_path = tmp_path / "learned_chats.json"
    first.appid = "cli_bot"

    assert first._remember_polling_chat_id("oc_learned", reason="test") is True

    second = LarkPlatformAdapter.__new__(LarkPlatformAdapter)
    second.polling_fallback_chat_ids = ["oc_configured"]
    second.polling_chat_store_path = tmp_path / "learned_chats.json"
    second._load_persisted_polling_chat_ids()

    assert second.polling_fallback_chat_ids == ["oc_configured", "oc_learned"]


def test_lark_polling_loads_p2p_chat_ids_without_oc_prefix(tmp_path) -> None:
    store_path = tmp_path / "learned_chats.json"
    store_path.write_text(
        json.dumps({"chat_ids": ["p2p_chat_1", "oc_group"]}),
        encoding="utf-8",
    )
    adapter = LarkPlatformAdapter.__new__(LarkPlatformAdapter)
    adapter.polling_fallback_chat_ids = []
    adapter.polling_chat_store_path = store_path

    adapter._load_persisted_polling_chat_ids()

    assert adapter.polling_fallback_chat_ids == ["p2p_chat_1", "oc_group"]


def test_lark_polling_persists_existing_configured_chat(tmp_path) -> None:
    adapter = LarkPlatformAdapter.__new__(LarkPlatformAdapter)
    adapter.polling_fallback_enabled = True
    adapter.polling_fallback_chat_ids = ["oc_configured"]
    adapter.polling_chat_store_path = tmp_path / "learned_chats.json"
    adapter.appid = "cli_bot"

    assert adapter._remember_polling_chat_id("oc_configured", reason="startup") is False

    payload = json.loads(adapter.polling_chat_store_path.read_text(encoding="utf-8"))
    assert payload["chat_ids"] == ["oc_configured"]


def test_lark_polling_uses_independent_cursor_per_chat(tmp_path) -> None:
    async def run() -> None:
        adapter = LarkPlatformAdapter.__new__(LarkPlatformAdapter)
        adapter.config = {"id": "lark-test"}
        adapter.polling_fallback_page_size = 20
        adapter._polling_next_start_time = 800
        adapter._polling_next_start_times = {"oc_first": 800, "oc_second": 800}
        adapter.message_id_timestamps = {}
        adapter.message_id_store_path = tmp_path / "seen.json"
        adapter._message_id_store_loaded = True

        requests: list[tuple[str, str]] = []

        class Response:
            def __init__(self, item):
                self.data = SimpleNamespace(
                    items=[item],
                    page_token="",
                    has_more=False,
                )
                self.code = 0
                self.msg = "success"

            def success(self):
                return True

        class MessageApi:
            async def alist(self, request):
                requests.append((request.container_id, request.start_time))
                item = SimpleNamespace(
                    message_id=f"om_{request.container_id}",
                    create_time=(
                        "1000" if request.container_id == "oc_first" else "900"
                    ),
                )
                return Response(item)

        adapter.lark_api = SimpleNamespace(
            im=SimpleNamespace(v1=SimpleNamespace(message=MessageApi()))
        )

        async def build_message(item):
            return SimpleNamespace(message_id=item.message_id, session_id="ou_user")

        handled = []

        async def handle_msg(abm):
            handled.append(abm)

        adapter._build_polled_message = build_message
        adapter.handle_msg = handle_msg

        await adapter._poll_lark_chat_messages("oc_first")
        await adapter._poll_lark_chat_messages("oc_second")

        assert requests == [("oc_first", "800"), ("oc_second", "800")]
        assert adapter._polling_next_start_times["oc_first"] == 995
        assert adapter._polling_next_start_times["oc_second"] == 895
        assert adapter._polling_next_start_time == 895
        assert [item.message_id for item in handled] == ["om_oc_first", "om_oc_second"]

    asyncio.run(run())


def test_lark_polling_does_not_learn_when_disabled() -> None:
    adapter = LarkPlatformAdapter.__new__(LarkPlatformAdapter)
    adapter.polling_fallback_enabled = False
    adapter.polling_fallback_chat_ids = []

    assert adapter._remember_polling_chat_id("oc_new", reason="test") is False
    assert adapter.polling_fallback_chat_ids == []


def test_lark_message_seen_cache_survives_adapter_restart(tmp_path) -> None:
    first = LarkPlatformAdapter.__new__(LarkPlatformAdapter)
    first.message_id_timestamps = {}
    first.message_id_store_path = tmp_path / "seen.json"
    first._message_id_store_loaded = True

    assert first._mark_message_id_seen("om_seen") is True

    second = LarkPlatformAdapter.__new__(LarkPlatformAdapter)
    second.message_id_timestamps = {}
    second.message_id_store_path = tmp_path / "seen.json"
    second._message_id_store_loaded = False

    assert second._mark_message_id_seen("om_seen") is False


def test_lark_initial_polling_starts_now_without_persisted_seen_cache() -> None:
    adapter = LarkPlatformAdapter.__new__(LarkPlatformAdapter)
    adapter.polling_fallback_backfill_seconds = 600

    before = int(time.time())
    start_time = adapter._initial_polling_start_time(has_persisted_seen_messages=False)
    after = int(time.time())

    assert before <= start_time <= after


def test_lark_initial_polling_uses_backfill_with_persisted_seen_cache() -> None:
    adapter = LarkPlatformAdapter.__new__(LarkPlatformAdapter)
    adapter.polling_fallback_backfill_seconds = 600

    before = int(time.time()) - 600
    start_time = adapter._initial_polling_start_time(has_persisted_seen_messages=True)
    after = int(time.time()) - 600

    assert before <= start_time <= after


def test_lark_terminate_cancels_sdk_cache_cron() -> None:
    async def run() -> None:
        adapter = LarkPlatformAdapter.__new__(LarkPlatformAdapter)
        adapter._polling_fallback_task = None
        adapter.connection_mode = "webhook"
        cache_cron = asyncio.create_task(asyncio.sleep(3600))
        adapter.client = SimpleNamespace(
            _cache=SimpleNamespace(_cron=cache_cron),
        )

        await adapter.terminate()

        assert cache_cron.done()
        assert cache_cron.cancelled()

    asyncio.run(run())


def test_lark_terminate_disables_socket_reconnect_before_disconnect() -> None:
    async def run() -> None:
        adapter = LarkPlatformAdapter.__new__(LarkPlatformAdapter)
        adapter._polling_fallback_task = None
        adapter.connection_mode = "socket"
        adapter.client = SimpleNamespace(
            _auto_reconnect=True,
            _disconnect=AsyncMock(),
            _cache=None,
        )

        await adapter.terminate()

        assert adapter.client._auto_reconnect is False
        adapter.client._disconnect.assert_awaited_once()

    asyncio.run(run())
