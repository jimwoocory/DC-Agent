from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dc_engines"))

from dc_engines.god_mode import (  # noqa: E402
    GodModeConfig,
    GodModeEngine,
    GodModeRequest,
)

from data.plugins.god_mode_plugin.main import (  # noqa: E402
    CARD_SOURCE,
    GodModePlugin,
    build_god_mode_approval_card,
)


class FakeContext:
    def __init__(self, cfg: dict, **attrs) -> None:
        self._cfg = cfg
        self.feishu_streamers = {}
        for key, value in attrs.items():
            setattr(self, key, value)

    def get_config(self):
        return self._cfg


class FakeEvent:
    def __init__(
        self,
        *,
        text: str,
        sender_id: str = "ou_admin",
        admin: bool = True,
        trusted_card_action: bool = False,
    ) -> None:
        self.message_str = text
        self.unified_msg_origin = f"lark:PrivateMessage:{sender_id}"
        self.message_id = "om_message"
        self.open_id = sender_id
        self.message_obj = SimpleNamespace(
            is_card_action=trusted_card_action,
            raw_message=SimpleNamespace(chat_id="oc_private"),
        )
        self.is_card_action = trusted_card_action
        self.results: list[object] = []
        self.stopped = False
        self.extras = {"feishu_ingress_audit_id": "audit_feishu_1"}
        self._sender_id = sender_id
        self._admin = admin

    def get_sender_id(self):
        return self._sender_id

    def get_platform_id(self):
        return "lark"

    def get_extra(self, key: str, default=None):
        return self.extras.get(key, default)

    def is_admin(self):
        return self._admin

    def set_result(self, result):
        self.results.append(result)

    def stop_event(self):
        self.stopped = True

    @property
    def text_result(self) -> str:
        if not self.results:
            return ""
        return str(getattr(self.results[-1], "chain", "") or self.results[-1])


def _plugin(tmp_path: Path) -> GodModePlugin:
    return GodModePlugin(
        FakeContext(
            {
                "owners": ["ou_admin"],
                "audit_db_path": str(tmp_path / "god_mode.db"),
            }
        )
    )


class FakeHarnessEngine:
    def __init__(self) -> None:
        self.requests = []

    async def create_task(self, request):
        self.requests.append(request)
        return SimpleNamespace(task_id=f"task_{len(self.requests)}")


def test_approval_card_contains_required_value_payload(tmp_path: Path) -> None:
    engine = GodModeEngine(tmp_path / "god_mode.db")
    config = GodModeConfig.from_dict(
        {
            "owners": ["ou_admin"],
            "audit_db_path": str(tmp_path / "god_mode.db"),
        }
    )
    run = engine.plan(
        GodModeRequest(
            text="/god 发送飞书卡片通知项目群",
            actor="ou_admin",
            sender_id="ou_admin",
            actor_is_admin=True,
        ),
        config,
    )

    card = build_god_mode_approval_card(run)
    actions = card["elements"][1]["actions"]
    approve_value = actions[0]["value"]
    reject_value = actions[1]["value"]

    assert approve_value == {
        "source": CARD_SOURCE,
        "run_id": run.run_id,
        "action_id": "action_1",
        "decision": "approve",
    }
    assert reject_value["source"] == CARD_SOURCE
    assert reject_value["decision"] == "reject"


def test_admin_read_only_god_command_returns_result(tmp_path: Path) -> None:
    plugin = _plugin(tmp_path)
    event = FakeEvent(text="/god 查询当前 Harness 状态")

    asyncio.run(plugin.god_command(event))

    assert "status: completed" in event.text_result
    assert "dc_agent_check_task_status" in event.text_result
    assert event.stopped is False


def test_non_admin_god_command_is_refused(tmp_path: Path) -> None:
    plugin = _plugin(tmp_path)
    event = FakeEvent(
        text="/god 发送飞书卡片通知项目群",
        sender_id="ou_user",
        admin=False,
    )

    asyncio.run(plugin.god_command(event))

    assert "权限不足" in event.text_result


def test_admin_side_effect_request_sends_approval_card(
    tmp_path: Path, monkeypatch
) -> None:
    plugin = _plugin(tmp_path)
    event = FakeEvent(text="/god 发送飞书卡片通知项目群")
    sent: list[str] = []

    async def fake_send_approval_card(_event, run):
        sent.append(run.run_id)
        return True

    monkeypatch.setattr(plugin, "_send_approval_card", fake_send_approval_card)

    asyncio.run(plugin.god_command(event))

    assert sent
    assert event.stopped is True
    assert event.results == []


def test_trusted_card_approval_executes_only_once(tmp_path: Path) -> None:
    plugin = _plugin(tmp_path)
    plan_event = FakeEvent(text="/god 发送飞书卡片通知项目群")

    async def fake_send_approval_card(_event, _run):
        return False

    plugin._send_approval_card = fake_send_approval_card  # type: ignore[method-assign]
    asyncio.run(plugin.god_command(plan_event))
    run_id = plugin.god_engine.latest_run().run_id
    payload = "__card_action__:" + json.dumps(
        {
            "value": {
                "source": CARD_SOURCE,
                "run_id": run_id,
                "action_id": "action_1",
                "decision": "approve",
            }
        },
        ensure_ascii=False,
    )
    first = FakeEvent(text=payload, trusted_card_action=True)
    second = FakeEvent(text=payload, trusted_card_action=True)

    asyncio.run(plugin.handle_card_action(first))
    asyncio.run(plugin.handle_card_action(second))

    events = plugin.god_engine.audit_events(run_id)
    assert [event["event"] for event in events].count("action_executed") == 1
    assert "status: completed" in first.text_result


def test_untrusted_god_card_action_is_swallowed(tmp_path: Path) -> None:
    plugin = _plugin(tmp_path)
    payload = "__card_action__:" + json.dumps(
        {
            "value": {
                "source": CARD_SOURCE,
                "run_id": "god_unknown",
                "action_id": "action_1",
                "decision": "approve",
            }
        },
        ensure_ascii=False,
    )
    event = FakeEvent(text=payload, trusted_card_action=False)

    asyncio.run(plugin.handle_card_action(event))

    assert event.stopped is True
    assert event.results == []


def test_approved_workflow_action_creates_harness_task_once(tmp_path: Path) -> None:
    harness_engine = FakeHarnessEngine()
    plugin = GodModePlugin(
        FakeContext(
            {
                "owners": ["ou_admin"],
                "audit_db_path": str(tmp_path / "god_mode.db"),
            },
            harness_engine=harness_engine,
        )
    )
    plan_event = FakeEvent(text="/god 启动 Hermes 工作流")

    async def fake_send_approval_card(_event, _run):
        return False

    plugin._send_approval_card = fake_send_approval_card  # type: ignore[method-assign]
    asyncio.run(plugin.god_command(plan_event))
    run_id = plugin.god_engine.latest_run().run_id
    payload = "__card_action__:" + json.dumps(
        {
            "value": {
                "source": CARD_SOURCE,
                "run_id": run_id,
                "action_id": "action_1",
                "decision": "approve",
            }
        },
        ensure_ascii=False,
    )
    first = FakeEvent(text=payload, trusted_card_action=True)
    second = FakeEvent(text=payload, trusted_card_action=True)

    asyncio.run(plugin.handle_card_action(first))
    asyncio.run(plugin.handle_card_action(second))

    assert len(harness_engine.requests) == 1
    assert harness_engine.requests[0].domain == "god_mode"
    assert harness_engine.requests[0].payload["source"] == "god_mode"
    assert "Harness 任务已创建" in first.text_result
