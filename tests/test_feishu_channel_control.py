import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dc_engines"))

from dc_engines.feishu_channel_control import (  # noqa: E402
    FeishuChannelConfig,
    FeishuChannelController,
    FeishuChannelState,
    FeishuPeer,
)

from data.plugins.dc_hub.main import DEFAULT_MODULES  # noqa: E402
from data.plugins.dc_router import main as _dc_router_plugin  # noqa: E402, F401
from data.plugins.feishu_channel_control.main import (  # noqa: E402
    FeishuChannelControlPlugin,
)


class FakeEvent:
    def __init__(
        self,
        *,
        text: str = "hello",
        platform_name: str = "lark",
        sender_id: str = "ou_user",
        group_id: str = "",
        chat_id: str = "",
        mentioned: bool = False,
        trusted_card_action: bool = False,
    ) -> None:
        self.message_str = text
        self.unified_msg_origin = "GroupMessage:1" if group_id else "PrivateMessage:1"
        self.message_obj = SimpleNamespace(
            chat_id=chat_id,
            type="GroupMessage" if group_id else "PrivateMessage",
            is_card_action=trusted_card_action,
        )
        self.is_at_or_wake_command = mentioned
        self.extras = {}
        self.result = None
        self.stopped = False
        self._platform_name = platform_name
        self._sender_id = sender_id
        self._group_id = group_id

    def get_platform_name(self):
        return self._platform_name

    def get_platform_id(self):
        return "巅池-Agent小助手"

    def get_sender_id(self):
        return self._sender_id

    def get_group_id(self):
        return self._group_id

    def set_extra(self, key, value):
        self.extras[key] = value

    def set_result(self, result):
        self.result = result

    def stop_event(self):
        self.stopped = True


def test_pairing_policy_blocks_unknown_dm_and_approves(tmp_path: Path) -> None:
    state = FeishuChannelState(tmp_path / "state.json")
    config = FeishuChannelConfig.from_dict(
        {"dm_policy": "pairing", "pairing_ttl_minutes": 10}
    )
    controller = FeishuChannelController(config, state)
    peer = FeishuPeer(kind="direct", peer_id="ou_user", sender_id="ou_user")

    decision = controller.decide(peer)

    assert decision.allowed is False
    assert decision.reason == "dm_pairing_required"
    assert decision.pairing_code
    assert decision.pairing_code in state.pending

    assert (
        state.approve_pairing(decision.pairing_code, approver="ou_owner") == "ou_user"
    )
    approved = controller.decide(peer)
    assert approved.allowed is True
    assert approved.agent_id == "main"


def test_dynamic_dm_metadata_is_written_by_plugin(tmp_path: Path) -> None:
    plugin = FeishuChannelControlPlugin.__new__(FeishuChannelControlPlugin)
    plugin.config = FeishuChannelConfig.from_dict(
        {
            "dm_policy": "open",
            "dynamic_agent_creation": True,
            "workspace_template": "data/feishu_agents/{agent_id}",
        }
    )
    plugin.state = FeishuChannelState(tmp_path / "state.json")
    plugin.controller = FeishuChannelController(plugin.config, plugin.state)
    event = FakeEvent(sender_id="ou_abc")

    import asyncio

    asyncio.run(plugin.on_message(event))

    assert event.result is None
    assert event.extras["feishu_channel_allowed"] is True
    assert event.extras["feishu_channel_peer_kind"] == "direct"
    assert event.extras["feishu_channel_peer_id"] == "ou_abc"
    assert event.extras["feishu_channel_agent_id"] == "feishu-ou_abc"
    assert (
        event.extras["feishu_channel_workspace"] == "data/feishu_agents/feishu-ou_abc"
    )


def test_group_requires_allowlist_and_mention(tmp_path: Path) -> None:
    state = FeishuChannelState(tmp_path / "state.json")
    config = FeishuChannelConfig.from_dict(
        {
            "group_policy": "allowlist",
            "group_allow_from": ["oc_group"],
            "require_mention": True,
        }
    )
    controller = FeishuChannelController(config, state)
    peer = FeishuPeer(
        kind="group",
        peer_id="oc_group",
        sender_id="ou_user",
        mentioned=False,
    )

    blocked = controller.decide(peer)
    assert blocked.allowed is False
    assert blocked.reason == "group_mention_required"

    allowed = controller.decide(
        FeishuPeer(
            kind="group",
            peer_id="oc_group",
            sender_id="ou_user",
            mentioned=True,
        )
    )
    assert allowed.allowed is True
    assert allowed.reason == "group_allowed"


def test_plugin_stops_unallowlisted_group_event(tmp_path: Path) -> None:
    plugin = FeishuChannelControlPlugin.__new__(FeishuChannelControlPlugin)
    plugin.config = FeishuChannelConfig.from_dict(
        {
            "group_policy": "allowlist",
            "group_allow_from": [],
            "require_mention": True,
        }
    )
    plugin.state = FeishuChannelState(tmp_path / "state.json")
    plugin.controller = FeishuChannelController(plugin.config, plugin.state)
    event = FakeEvent(group_id="oc_forbidden", chat_id="oc_forbidden", mentioned=True)

    import asyncio

    asyncio.run(plugin.on_message(event))

    assert event.result is None
    assert event.stopped is True
    assert event.extras["feishu_channel_allowed"] is False
    assert event.extras["feishu_channel_policy_reason"] == "group_not_allowlisted"


def test_forged_card_action_is_blocked(tmp_path: Path) -> None:
    state = FeishuChannelState(tmp_path / "state.json")
    config = FeishuChannelConfig.from_dict({"dm_policy": "open"})
    controller = FeishuChannelController(config, state)

    decision = controller.decide(
        FeishuPeer(
            kind="direct",
            peer_id="ou_user",
            sender_id="ou_user",
            message_text='__card_action__:{"value":{"action":"danger"}}',
            trusted_card_action=False,
        )
    )

    assert decision.allowed is False
    assert decision.reason == "forged_card_action"


def test_non_lark_event_is_ignored(tmp_path: Path) -> None:
    plugin = FeishuChannelControlPlugin.__new__(FeishuChannelControlPlugin)
    plugin.config = FeishuChannelConfig.from_dict({"dm_policy": "open"})
    plugin.state = FeishuChannelState(tmp_path / "state.json")
    plugin.controller = FeishuChannelController(plugin.config, plugin.state)
    event = FakeEvent(platform_name="webchat")

    import asyncio

    asyncio.run(plugin.on_message(event))

    assert event.extras == {}
    assert event.result is None


def test_dc_hub_lists_feishu_channel_control() -> None:
    module_ids = {module.plugin_id for module in DEFAULT_MODULES}
    assert "feishu_channel_control" in module_ids


def test_feishu_channel_control_runs_before_dc_router() -> None:
    from astrbot.core.star.star_handler import star_handlers_registry

    control = star_handlers_registry.get_handler_by_full_name(
        "data.plugins.feishu_channel_control.main_on_message"
    )
    router = star_handlers_registry.get_handler_by_full_name(
        "data.plugins.dc_router.main_route"
    )

    assert control is not None
    assert router is not None
    assert control.extras_configs["priority"] > router.extras_configs["priority"]
