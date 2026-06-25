import importlib
import sqlite3
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
from data.plugins.feishu_channel_control import main as _feishu_channel_control_plugin
from data.plugins.feishu_channel_control.main import (  # noqa: E402
    FeishuChannelControlPlugin,
)


class FakeEvent:
    def __init__(
        self,
        *,
        text: str = "hello",
        platform_name: str = "lark",
        platform_id: str = "巅池-Agent小助手",
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
        self._platform_id = platform_id
        self._sender_id = sender_id
        self._group_id = group_id

    def get_platform_name(self):
        return self._platform_name

    def get_platform_id(self):
        return self._platform_id

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return "测试同事"

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


def test_default_chat_entry_allows_first_direct_message(tmp_path: Path) -> None:
    state = FeishuChannelState(tmp_path / "state.json")
    config = FeishuChannelConfig.from_dict({})
    controller = FeishuChannelController(config, state)
    peer = FeishuPeer(
        kind="direct",
        peer_id="ou_first_time_colleague",
        sender_id="ou_first_time_colleague",
        message_text="小助手你好",
    )

    decision = controller.decide(peer)
    metadata = decision.metadata()

    assert config.dm_policy == "open"
    assert decision.allowed is True
    assert decision.reason == "dm_allowed"
    assert decision.stop_event is False
    assert decision.reply_text == ""
    assert state.is_approved("ou_first_time_colleague") is True
    assert metadata["dc_chat_entry_allowed"] is True
    assert metadata["dc_chat_entry_mode"] == "normal_chat"
    assert metadata["dc_chat_entry_identity_status"] == "partial"
    assert metadata["dc_chat_entry_fallback_policy"] == "normal_chat"


def test_plugin_writes_chat_entry_metadata_for_unknown_direct_message(
    tmp_path: Path,
) -> None:
    plugin = FeishuChannelControlPlugin.__new__(FeishuChannelControlPlugin)
    plugin.project_root = tmp_path
    plugin.config = FeishuChannelConfig.from_dict({})
    plugin.state = FeishuChannelState(tmp_path / "state.json")
    plugin.controller = FeishuChannelController(plugin.config, plugin.state)
    event = FakeEvent(sender_id="ou_unknown_colleague", chat_id="oc_p2p_chat")

    import asyncio

    asyncio.run(plugin.on_message(event))

    assert event.result is None
    assert event.stopped is False
    assert event.extras["feishu_channel_allowed"] is True
    assert event.extras["dc_chat_entry_allowed"] is True
    assert event.extras["dc_chat_entry_mode"] == "normal_chat"
    assert event.extras["dc_chat_entry_identity_status"] == "partial"
    assert event.extras["dc_chat_entry_fallback_policy"] == "normal_chat"


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


def test_open_dm_auto_approves_normal_colleague(tmp_path: Path) -> None:
    state = FeishuChannelState(tmp_path / "state.json")
    config = FeishuChannelConfig.from_dict({"dm_policy": "open"})
    controller = FeishuChannelController(config, state)
    peer = FeishuPeer(
        kind="direct",
        peer_id="ou_normal_colleague",
        sender_id="ou_normal_colleague",
    )

    decision = controller.decide(peer)

    assert decision.allowed is True
    assert state.is_approved("ou_normal_colleague") is True
    assert state.approved["ou_normal_colleague"]["source"] == "dm_open_auto"


def test_plugin_auto_approves_employee_directory_on_initialize(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    with sqlite3.connect(data_dir / "employees.db") as conn:
        conn.execute(
            """
            CREATE TABLE employees (
                open_id TEXT PRIMARY KEY,
                display_name TEXT DEFAULT '',
                department TEXT DEFAULT ''
            )
            """
        )
        conn.execute(
            """
            INSERT INTO employees(open_id, display_name, department)
            VALUES
                ('ou_qin_chunsi', '覃春丝', '策略部'),
                ('ou_caiting', '蔡挺', '数字化应用部')
            """
        )
        conn.commit()

    plugin = FeishuChannelControlPlugin.__new__(FeishuChannelControlPlugin)
    plugin.context = SimpleNamespace()
    plugin.project_root = tmp_path
    plugin.config = FeishuChannelConfig.from_dict({"dm_policy": "open"})
    plugin.state_path = tmp_path / "state.json"
    plugin.state = FeishuChannelState(tmp_path / "state.json")
    plugin.controller = FeishuChannelController(plugin.config, plugin.state)

    import asyncio

    asyncio.run(plugin.initialize())

    assert plugin.state.is_approved("ou_qin_chunsi") is True
    assert plugin.state.is_approved("ou_caiting") is True
    assert plugin.state.approved["ou_qin_chunsi"]["source"] == "employee_directory"
    assert plugin.state.approved["ou_qin_chunsi"]["display_name"] == "覃春丝"


def test_lark_p2p_oc_chat_id_is_still_direct(tmp_path: Path) -> None:
    plugin = FeishuChannelControlPlugin.__new__(FeishuChannelControlPlugin)
    plugin.config = FeishuChannelConfig.from_dict({"dm_policy": "open"})
    plugin.state = FeishuChannelState(tmp_path / "state.json")
    plugin.controller = FeishuChannelController(plugin.config, plugin.state)
    event = FakeEvent(sender_id="ou_colleague", chat_id="oc_p2p_chat")

    import asyncio

    asyncio.run(plugin.on_message(event))

    assert event.result is None
    assert event.extras["feishu_channel_allowed"] is True
    assert event.extras["feishu_channel_peer_kind"] == "direct"
    assert event.extras["feishu_channel_peer_id"] == "ou_colleague"


def test_plugin_recognizes_branded_feishu_platform_id(tmp_path: Path) -> None:
    plugin = FeishuChannelControlPlugin.__new__(FeishuChannelControlPlugin)
    plugin.project_root = tmp_path
    plugin.config = FeishuChannelConfig.from_dict({"dm_policy": "open"})
    plugin.state = FeishuChannelState(tmp_path / "state.json")
    plugin.controller = FeishuChannelController(plugin.config, plugin.state)
    event = FakeEvent(
        text="小助手，帮我看一下今天执行物料的问题。",
        platform_name="",
        sender_id="ou_execution_colleague",
        chat_id="oc_private_chat",
    )

    import asyncio

    asyncio.run(plugin.on_message(event))

    assert event.result is None
    assert event.extras["feishu_channel_allowed"] is True
    assert event.extras["feishu_ingress_audit_id"]
    with sqlite3.connect(tmp_path / "data" / "ai_inbox.db") as conn:
        row = conn.execute(
            "SELECT sender_id, text, allowed FROM feishu_ingress_audit"
        ).fetchone()
    assert row == (
        "ou_execution_colleague",
        "小助手，帮我看一下今天执行物料的问题。",
        1,
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
    plugin.project_root = tmp_path
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
    assert event.extras["feishu_ingress_audit_id"]
    with sqlite3.connect(tmp_path / "data" / "ai_inbox.db") as conn:
        row = conn.execute(
            """
            SELECT sender_id, sender_name, text, allowed, policy_reason
            FROM feishu_ingress_audit
            """
        ).fetchone()
    assert row == (
        "ou_user",
        "测试同事",
        "hello",
        0,
        "group_not_allowlisted",
    )


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
    assert decision.metadata()["dc_chat_entry_mode"] == "sensitive_action_candidate"
    assert decision.metadata()["dc_chat_entry_fallback_policy"] == "fail_closed"


def test_non_lark_event_is_ignored(tmp_path: Path) -> None:
    plugin = FeishuChannelControlPlugin.__new__(FeishuChannelControlPlugin)
    plugin.config = FeishuChannelConfig.from_dict({"dm_policy": "open"})
    plugin.state = FeishuChannelState(tmp_path / "state.json")
    plugin.controller = FeishuChannelController(plugin.config, plugin.state)
    event = FakeEvent(
        platform_name="webchat", platform_id="webchat", sender_id="web_user"
    )

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
    if control is None or router is None:
        importlib.reload(_feishu_channel_control_plugin)
        importlib.reload(_dc_router_plugin)
        control = star_handlers_registry.get_handler_by_full_name(
            "data.plugins.feishu_channel_control.main_on_message"
        )
        router = star_handlers_registry.get_handler_by_full_name(
            "data.plugins.dc_router.main_route"
        )

    assert control is not None
    assert router is not None
    assert control.extras_configs["priority"] > router.extras_configs["priority"]
