import asyncio
import json
import time
from types import SimpleNamespace

from astrbot.core.platform.astrbot_message import MessageType
from astrbot.core.platform.sources.lark.lark_adapter import LarkPlatformAdapter


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
