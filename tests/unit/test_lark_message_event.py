from unittest.mock import MagicMock

from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.platform.sources.lark.lark_event import LarkMessageEvent


def _make_lark_event(message_id: str, session_id: str = "ou_user") -> LarkMessageEvent:
    message_obj = MagicMock()
    message_obj.message_id = message_id
    platform_meta = PlatformMetadata(
        id="lark",
        name="lark",
        description="Lark",
    )
    return LarkMessageEvent(
        message_str="hello",
        message_obj=message_obj,
        platform_meta=platform_meta,
        session_id=session_id,
        bot=MagicMock(),
    )


def test_lark_reply_args_use_real_open_message_id() -> None:
    event = _make_lark_event("om_real_message")

    assert event._reply_or_direct_args() == ("om_real_message", None, None)


def test_lark_reply_args_fall_back_to_open_id_for_synthetic_card_action_id() -> None:
    event = _make_lark_event("card_action_1234_ou_user", session_id="ou_user")

    assert event._reply_or_direct_args() == (None, "ou_user", "open_id")
