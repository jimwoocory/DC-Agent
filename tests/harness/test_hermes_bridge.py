from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from harness.callbacks import TaskCallbackPayload
from harness.hermes_bridge import HermesBridge, HermesTaskRequest
from harness.resources import ResourceConfig


class CapturingSink:
    def __init__(self) -> None:
        self.payloads: list[TaskCallbackPayload] = []

    async def send(self, payload: TaskCallbackPayload) -> None:
        self.payloads.append(payload)


class CapturingGate:
    def __init__(self) -> None:
        self.completed: list[dict[str, Any]] = []
        self.failed: list[dict[str, Any]] = []

    async def complete(
        self,
        job_id: str,
        *,
        result: dict[str, Any] | None = None,
        cooldown_seconds: int | None = None,
    ) -> None:
        self.completed.append(
            {
                "job_id": job_id,
                "result": result,
                "cooldown_seconds": cooldown_seconds,
            }
        )

    async def fail(
        self,
        job_id: str,
        error: str,
        *,
        retry_after_seconds: int | None = None,
    ) -> None:
        self.failed.append(
            {
                "job_id": job_id,
                "error": error,
                "retry_after_seconds": retry_after_seconds,
            }
        )


class FakeStream:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    async def readline(self) -> bytes:
        await asyncio.sleep(0)
        if not self._lines:
            return b""
        return self._lines.pop(0)


class FakeProcess:
    def __init__(
        self,
        *,
        returncode: int,
        stdout_lines: list[bytes] | None = None,
        stderr_lines: list[bytes] | None = None,
    ) -> None:
        self.returncode = returncode
        self.stdout = FakeStream(stdout_lines or [])
        self.stderr = FakeStream(stderr_lines or [])
        self.terminate_called = False
        self.kill_called = False

    async def wait(self) -> int:
        await asyncio.sleep(0)
        return self.returncode

    def terminate(self) -> None:
        self.terminate_called = True

    def kill(self) -> None:
        self.kill_called = True


class SlowProcess(FakeProcess):
    def __init__(self) -> None:
        super().__init__(returncode=-15)
        self.returncode = None
        self.stdout = FakeStream([])
        self.stderr = FakeStream([])

    async def wait(self) -> int:
        if self.terminate_called:
            self.returncode = -15
            return self.returncode
        await asyncio.sleep(10)
        return self.returncode


class FakeHermesResponseRequest:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.headers: dict[str, str] = {}

    async def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


class CapturingCardStreamer:
    def __init__(self, *, fail_finalize: bool = False) -> None:
        self.fail_finalize = fail_finalize
        self.updated: list[dict[str, Any]] = []
        self.finalized: list[dict[str, Any]] = []

    def get_stream(self, message_id: str):
        return SimpleNamespace(message_id=message_id, elapsed_sec=12.0)

    async def update(self, message_id: str, card: dict[str, Any]) -> bool:
        self.updated.append({"message_id": message_id, "card": card})
        return True

    async def finalize(self, message_id: str, card: dict[str, Any]) -> bool:
        if self.fail_finalize:
            raise RuntimeError("patch failed")
        self.finalized.append({"message_id": message_id, "card": card})
        return True


class CapturingHarnessEngine:
    def __init__(self) -> None:
        self.completed: list[dict[str, Any]] = []
        self.failed: list[dict[str, Any]] = []

    async def complete_task(self, task_id: str, *, result: dict[str, Any]) -> None:
        self.completed.append({"task_id": task_id, "result": result})

    async def fail_task(self, task_id: str, *, reason: str) -> None:
        self.failed.append({"task_id": task_id, "reason": reason})


def _load_plugin_module():
    module_path = (
        Path(__file__).resolve().parents[2]
        / "data"
        / "plugins"
        / "hermes_bridge"
        / "hermes_bridge.py"
    )
    spec = importlib.util.spec_from_file_location(
        "dc_hermes_bridge_plugin_test",
        module_path,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _plugin_with_card_streamer(streamer: CapturingCardStreamer, task_id: str):
    module = _load_plugin_module()
    engine = CapturingHarnessEngine()
    context = SimpleNamespace(
        feishu_stream_map={
            task_id: {
                "message_id": "msg-card-1",
                "brief": "深度分析",
                "reasoning_tier": "high",
                "platform_id": "巅池-Agent小助手",
            }
        },
        feishu_streamers={"巅池-Agent小助手": streamer},
        platform_manager=SimpleNamespace(platform_insts=[]),
        harness_engine=engine,
    )
    plugin = object.__new__(module.HermesBridgePlugin)
    plugin.context = context
    plugin.hermes_secret = "test-secret"
    plugin._umo_cache = {}
    plugin._finalized_task_ids = set()
    plugin.session_router = SimpleNamespace(
        get_platform_user_by_session=lambda session_key: None
    )
    plugin._dlq_logger = SimpleNamespace(log=lambda payload: None)
    plugin._callback_dispatcher = SimpleNamespace(send_with_retry=None)
    return plugin, context, engine


class FakeChatEvent:
    def __init__(
        self,
        *,
        umo: str,
        is_group: bool,
        platform_id: str = "巅池-推广01",
        sender_name: str = "蔡挺",
        sender_id: str = "admin-user",
        is_admin: bool = True,
    ) -> None:
        self.unified_msg_origin = umo
        self._is_group = is_group
        self._platform_id = platform_id
        self._sender_name = sender_name
        self._sender_id = sender_id
        self._is_admin = is_admin
        self.sent: list[Any] = []

    def is_group(self) -> bool:
        return self._is_group

    def get_sender_name(self) -> str:
        return self._sender_name

    def get_sender_id(self) -> str:
        return self._sender_id

    def get_platform_id(self) -> str:
        return self._platform_id

    def is_admin(self) -> bool:
        return self._is_admin

    async def send(self, message: Any) -> None:
        self.sent.append(message)


def _plugin_for_distillation(base_dir: Path | None = None):
    module = _load_plugin_module()
    plugin = object.__new__(module.HermesBridgePlugin)
    plugin.topic_distill_enabled = True
    plugin.topic_discussion_limit = 30
    plugin.skill_admin_only = True
    plugin.skill_admin_user_ids = set()
    plugin.protected_identity_names = set()
    plugin.protected_identity_user_ids = set()
    plugin.skill_ops_require_confirm = True
    plugin._conversation_discussion_cache = module.defaultdict(
        lambda: module.deque(maxlen=plugin.topic_discussion_limit)
    )
    if base_dir is not None:
        plugin.skill_bundle_base_dir = base_dir
    return plugin, module


@pytest.mark.asyncio
async def test_records_normal_group_chat_for_conversation_distillation():
    plugin, module = _plugin_for_distillation()
    event = FakeChatEvent(umo="umo-group-1", is_group=True, sender_name="市场同事")

    await plugin._record_conversation_discussion(
        event,
        "user-1",
        "老板要看的结论应该是预算、周期和风险，员工执行会卡在人手。",
    )

    snapshot = plugin._conversation_discussion_snapshot(event)
    distillation = plugin._distill_grey_topic(
        module._DistillationTopic("umo-group-1", "当前群聊"),
        snapshot,
        source_scope="normal_chat",
        conversation_type="group",
    )

    assert distillation["source_scope"] == "normal_chat"
    assert distillation["conversation_type"] == "group"
    assert distillation["boss_success_criteria"]
    assert distillation["department_constraints"]
    assert "【会话蒸馏摘要】" in plugin._format_distillation(distillation)


@pytest.mark.asyncio
async def test_records_private_chat_as_private_distillation_section():
    plugin, module = _plugin_for_distillation()
    event = FakeChatEvent(umo="umo-private-1", is_group=False, sender_name="杨总")

    await plugin._record_conversation_discussion(
        event,
        "boss-1",
        "我这边担心这个方案不够落地，先别发群里，结论要直接。",
    )

    snapshot = plugin._conversation_discussion_snapshot(event)
    distillation = plugin._distill_grey_topic(
        module._DistillationTopic("umo-private-1", "当前私聊"),
        snapshot,
        source_scope="normal_chat",
        conversation_type="private",
    )
    rendered = plugin._format_distillation(distillation)

    assert distillation["private_concerns"]
    assert "私聊关注/个人诉求" in rendered
    assert "【会话蒸馏摘要】" in rendered


@pytest.mark.asyncio
async def test_chat_distill_command_does_not_depend_on_topic_workflow():
    plugin, _ = _plugin_for_distillation()
    plugin.conversation_distill_enabled = True
    event = FakeChatEvent(umo="umo-group-command", is_group=True)

    await plugin._record_conversation_discussion(
        event,
        "user-1",
        "老板要看的结论应该是预算，员工执行要有步骤。",
    )

    handled = await plugin._handle_conversation_distill_command(event, "/chat distill")

    assert handled is True
    assert event.sent


@pytest.mark.asyncio
async def test_chat_create_boss_writes_skill_bundle(tmp_path: Path):
    plugin, _ = _plugin_for_distillation(tmp_path)
    event = FakeChatEvent(umo="umo-boss-skill", is_group=True, sender_name="杨总")

    await plugin._record_conversation_discussion(
        event,
        "boss-1",
        "老板要看的结论应该是预算、周期和风险，不满意就继续深挖。",
    )

    handled = await plugin._handle_conversation_distill_command(
        event,
        "/chat create-boss 杨总",
    )

    boss_dir = tmp_path / "bosses" / "杨总"
    assert handled is True
    assert (boss_dir / "SKILL.md").exists()
    assert (boss_dir / "judgment.md").exists()
    assert (boss_dir / "management.md").exists()
    assert (boss_dir / "persona.md").exists()
    assert (boss_dir / "meta.json").exists()


@pytest.mark.asyncio
async def test_chat_create_boss_updates_version_and_backup(tmp_path: Path):
    plugin, _ = _plugin_for_distillation(tmp_path)
    event = FakeChatEvent(umo="umo-boss-version", is_group=True, sender_name="杨总")

    await plugin._record_conversation_discussion(
        event,
        "boss-1",
        "老板要看的结论应该是预算。",
    )
    await plugin._handle_conversation_distill_command(event, "/chat create-boss 杨总")
    await plugin._record_conversation_discussion(
        event,
        "boss-1",
        "老板不满意时需要继续深挖风险。",
    )
    await plugin._handle_conversation_distill_command(event, "/chat create-boss 杨总")

    boss_dir = tmp_path / "bosses" / "杨总"
    meta = json.loads((boss_dir / "meta.json").read_text(encoding="utf-8"))
    backups = list((boss_dir / "versions").iterdir())
    assert meta["version"] == "v2"
    assert backups
    assert (backups[0] / "SKILL.md").exists()


@pytest.mark.asyncio
async def test_chat_rollback_boss_restores_previous_version(tmp_path: Path):
    plugin, _ = _plugin_for_distillation(tmp_path)
    event = FakeChatEvent(umo="umo-boss-rollback", is_group=True, sender_name="杨总")

    await plugin._record_conversation_discussion(
        event,
        "boss-1",
        "老板要看的结论应该是预算。",
    )
    await plugin._handle_conversation_distill_command(event, "/chat create-boss 杨总")
    v1_skill = (tmp_path / "bosses" / "杨总" / "SKILL.md").read_text(encoding="utf-8")
    await plugin._record_conversation_discussion(
        event,
        "boss-1",
        "老板不满意时需要继续深挖风险。",
    )
    await plugin._handle_conversation_distill_command(event, "/chat create-boss 杨总")

    handled = await plugin._handle_conversation_distill_command(
        event,
        "/chat boss-rollback 杨总 v1 --confirm",
    )
    restored = (tmp_path / "bosses" / "杨总" / "SKILL.md").read_text(encoding="utf-8")

    assert handled is True
    assert restored == v1_skill


@pytest.mark.asyncio
async def test_chat_create_colleague_writes_skill_bundle(tmp_path: Path):
    plugin, _ = _plugin_for_distillation(tmp_path)
    event = FakeChatEvent(
        umo="umo-colleague-skill", is_group=True, sender_name="市场同事"
    )

    await plugin._record_conversation_discussion(
        event,
        "user-1",
        "同事执行会卡在人手和周期，需要把客户资料、流程和标准话术整理出来。",
    )

    handled = await plugin._handle_conversation_distill_command(
        event,
        "/chat create-colleague 市场同事",
    )

    colleague_dir = tmp_path / "colleagues" / "市场同事"
    assert handled is True
    assert (colleague_dir / "SKILL.md").exists()
    assert (colleague_dir / "work.md").exists()
    assert (colleague_dir / "persona.md").exists()
    assert (colleague_dir / "meta.json").exists()


@pytest.mark.asyncio
async def test_chat_create_colleague_rejects_protected_identity(tmp_path: Path):
    plugin, _ = _plugin_for_distillation(tmp_path)
    plugin.protected_identity_names = {"蔡挺"}
    plugin.protected_identity_user_ids = {"ou_129defaa7d62fdb15ffc1eab436791d6"}
    plugin.skill_admin_user_ids.update(plugin.protected_identity_user_ids)
    event = FakeChatEvent(
        umo="umo-protected-identity",
        is_group=True,
        sender_name="蔡挺",
        sender_id="ou_129defaa7d62fdb15ffc1eab436791d6",
    )

    await plugin._record_conversation_discussion(
        event,
        "ou_129defaa7d62fdb15ffc1eab436791d6",
        "我是蔡挺，负责 DC-Agent 的产品设计和验收规则。",
    )
    handled = await plugin._handle_conversation_distill_command(
        event,
        "/chat create-colleague 蔡挺",
    )

    rendered = str(event.sent[-1])
    assert handled is True
    assert "受保护身份" in rendered
    assert not (tmp_path / "colleagues" / "蔡挺").exists()


@pytest.mark.asyncio
async def test_protected_identity_user_id_is_skill_admin(tmp_path: Path):
    plugin, _ = _plugin_for_distillation(tmp_path)
    protected_open_id = "ou_129defaa7d62fdb15ffc1eab436791d6"
    plugin.protected_identity_user_ids = {protected_open_id}
    plugin.skill_admin_user_ids.update(plugin.protected_identity_user_ids)
    event = FakeChatEvent(
        umo="umo-protected-admin",
        is_group=True,
        sender_name="蔡挺",
        sender_id=protected_open_id,
        is_admin=False,
    )

    assert await plugin._require_skill_admin(event) is True
    assert event.sent == []


@pytest.mark.asyncio
async def test_chat_list_generated_skill_bundles(tmp_path: Path):
    plugin, _ = _plugin_for_distillation(tmp_path)
    event = FakeChatEvent(umo="umo-list-skills", is_group=True, sender_name="杨总")

    await plugin._record_conversation_discussion(
        event,
        "boss-1",
        "老板要看的结论应该是预算。",
    )
    await plugin._handle_conversation_distill_command(event, "/chat create-boss 杨总")
    await plugin._handle_conversation_distill_command(event, "/chat list-bosses")

    rendered = str(event.sent[-1])
    assert "杨总" in rendered
    assert "v1" in rendered


@pytest.mark.asyncio
async def test_chat_inspect_generated_boss_skill_bundle(tmp_path: Path):
    plugin, _ = _plugin_for_distillation(tmp_path)
    event = FakeChatEvent(umo="umo-inspect-boss", is_group=True, sender_name="杨总")

    await plugin._record_conversation_discussion(
        event,
        "boss-1",
        "老板要看的结论应该是预算、周期和风险。",
    )
    await plugin._handle_conversation_distill_command(event, "/chat create-boss 杨总")
    handled = await plugin._handle_conversation_distill_command(
        event,
        "/chat inspect-boss 杨总",
    )

    rendered = str(event.sent[-1])
    assert handled is True
    assert "老板 skill" in rendered
    assert "version: v1" in rendered
    assert "SKILL.md" in rendered
    assert "judgment.md" in rendered


@pytest.mark.asyncio
async def test_chat_review_generated_boss_skill_bundle(tmp_path: Path):
    plugin, _ = _plugin_for_distillation(tmp_path)
    event = FakeChatEvent(umo="umo-review-boss", is_group=True, sender_name="杨总")

    await plugin._record_conversation_discussion(
        event,
        "boss-1",
        "老板要看的结论应该是预算、周期和风险。",
    )
    await plugin._handle_conversation_distill_command(event, "/chat create-boss 杨总")
    handled = await plugin._handle_conversation_distill_command(
        event,
        "/chat review-boss 杨总",
    )

    rendered = str(event.sent[-1])
    assert handled is True
    assert "质量审阅" in rendered
    assert "score:" in rendered
    assert "风险项" in rendered


@pytest.mark.asyncio
async def test_chat_correct_colleague_appends_correction_and_updates_meta(
    tmp_path: Path,
):
    plugin, _ = _plugin_for_distillation(tmp_path)
    event = FakeChatEvent(
        umo="umo-correct-colleague", is_group=True, sender_name="市场同事"
    )

    await plugin._record_conversation_discussion(
        event,
        "user-1",
        "同事执行会卡在人手和周期。",
    )
    await plugin._handle_conversation_distill_command(
        event,
        "/chat create-colleague 市场同事",
    )
    handled = await plugin._handle_conversation_distill_command(
        event,
        "/chat correct-colleague 市场同事 这个同事更适合先给模板再推进，不要直接催。",
    )

    colleague_dir = tmp_path / "colleagues" / "市场同事"
    corrections = (colleague_dir / "knowledge" / "corrections.md").read_text(
        encoding="utf-8"
    )
    meta = json.loads((colleague_dir / "meta.json").read_text(encoding="utf-8"))
    assert handled is True
    assert "先给模板再推进" in corrections
    assert meta["corrections_count"] == 1
    assert "knowledge/corrections.md" in meta["knowledge_sources"]


@pytest.mark.asyncio
async def test_chat_delete_colleague_soft_deletes_skill_bundle(tmp_path: Path):
    plugin, _ = _plugin_for_distillation(tmp_path)
    event = FakeChatEvent(
        umo="umo-delete-colleague", is_group=True, sender_name="市场同事"
    )

    await plugin._record_conversation_discussion(
        event,
        "user-1",
        "同事执行会卡在人手和周期。",
    )
    await plugin._handle_conversation_distill_command(
        event,
        "/chat create-colleague 市场同事",
    )
    colleague_dir = tmp_path / "colleagues" / "市场同事"
    assert colleague_dir.exists()

    handled = await plugin._handle_conversation_distill_command(
        event,
        "/chat delete-colleague 市场同事 --confirm",
    )

    assert handled is True
    assert not colleague_dir.exists()
    deleted = list((tmp_path / ".deleted").iterdir())
    assert deleted
    assert (deleted[0] / "SKILL.md").exists()


@pytest.mark.asyncio
async def test_chat_restore_deleted_colleague_skill_bundle(tmp_path: Path):
    plugin, _ = _plugin_for_distillation(tmp_path)
    event = FakeChatEvent(
        umo="umo-restore-colleague", is_group=True, sender_name="市场同事"
    )

    await plugin._record_conversation_discussion(
        event,
        "user-1",
        "同事执行会卡在人手和周期。",
    )
    await plugin._handle_conversation_distill_command(
        event,
        "/chat create-colleague 市场同事",
    )
    await plugin._handle_conversation_distill_command(
        event,
        "/chat delete-colleague 市场同事 --confirm",
    )

    await plugin._handle_conversation_distill_command(event, "/chat deleted-colleagues")
    listed = str(event.sent[-1])
    handled = await plugin._handle_conversation_distill_command(
        event,
        "/chat restore-colleague 市场同事",
    )

    colleague_dir = tmp_path / "colleagues" / "市场同事"
    assert "市场同事" in listed
    assert handled is True
    assert colleague_dir.exists()
    assert (colleague_dir / "SKILL.md").exists()
    assert not list((tmp_path / ".deleted").iterdir())


@pytest.mark.asyncio
async def test_chat_delete_skill_requires_admin_and_confirmation(tmp_path: Path):
    plugin, _ = _plugin_for_distillation(tmp_path)
    event = FakeChatEvent(
        umo="umo-delete-guard",
        is_group=True,
        sender_name="市场同事",
        is_admin=False,
    )

    await plugin._record_conversation_discussion(
        event,
        "user-1",
        "同事执行会卡在人手和周期。",
    )
    await plugin._handle_conversation_distill_command(
        event,
        "/chat create-colleague 市场同事",
    )
    handled = await plugin._handle_conversation_distill_command(
        event,
        "/chat delete-colleague 市场同事 --confirm",
    )

    rendered = str(event.sent[-1])
    assert handled is True
    assert "只有管理员" in rendered
    assert (tmp_path / "colleagues" / "市场同事").exists()


@pytest.mark.asyncio
async def test_chat_delete_skill_requires_confirmation(tmp_path: Path):
    plugin, _ = _plugin_for_distillation(tmp_path)
    event = FakeChatEvent(
        umo="umo-delete-confirm",
        is_group=True,
        sender_name="市场同事",
    )

    await plugin._record_conversation_discussion(
        event,
        "user-1",
        "同事执行会卡在人手和周期。",
    )
    await plugin._handle_conversation_distill_command(
        event,
        "/chat create-colleague 市场同事",
    )
    handled = await plugin._handle_conversation_distill_command(
        event,
        "/chat delete-colleague 市场同事",
    )

    rendered = str(event.sent[-1])
    assert handled is True
    assert "--confirm" in rendered
    assert (tmp_path / "colleagues" / "市场同事").exists()


@pytest.mark.asyncio
async def test_chat_list_bosses_sends_interactive_skill_list_card(tmp_path: Path):
    plugin, _ = _plugin_for_distillation(tmp_path)
    event = FakeChatEvent(umo="umo-card-list", is_group=True, sender_name="杨总")
    sent_cards: list[dict[str, Any]] = []

    async def fake_send_skill_card(event_arg: Any, card: dict) -> str:
        sent_cards.append(card)
        return "mid-skill-list"

    plugin._send_skill_card = fake_send_skill_card
    await plugin._record_conversation_discussion(
        event,
        "boss-1",
        "老板要看的结论应该是预算、周期和风险。",
    )
    await plugin._handle_conversation_distill_command(event, "/chat create-boss 杨总")
    handled = await plugin._handle_conversation_distill_command(
        event,
        "/chat list-bosses",
    )

    assert handled is True
    card = sent_cards[-1]
    assert card["header"]["title"]["content"] == "老板 Skill 列表"
    buttons = []
    for element in card["body"]["elements"]:
        for column in element.get("columns", []):
            for child in column.get("elements", []):
                if child.get("tag") == "button":
                    buttons.append(child)
    assert buttons[0]["value"]["source"] == "hermes_skill_card"
    assert buttons[0]["value"]["action"] == "inspect"
    assert buttons[0]["value"]["kind"] == "boss"
    assert buttons[0]["value"]["slug"] == "杨总"


@pytest.mark.asyncio
async def test_skill_card_delete_request_sends_confirm_card(tmp_path: Path):
    plugin, _ = _plugin_for_distillation(tmp_path)
    event = FakeChatEvent(umo="umo-card-delete", is_group=True, sender_name="杨总")
    event.is_card_action = True
    sent_cards: list[dict[str, Any]] = []

    async def fake_send_skill_card(event_arg: Any, card: dict) -> str:
        sent_cards.append(card)
        return "mid-confirm"

    plugin._send_skill_card = fake_send_skill_card
    payload = {
        "value": {
            "source": "hermes_skill_card",
            "action": "delete_request",
            "kind": "boss",
            "slug": "杨总",
            "version": "v1",
        }
    }
    handled = await plugin._handle_conversation_distill_command(
        event,
        "__card_action__:" + json.dumps(payload, ensure_ascii=False),
    )

    assert handled is True
    card = sent_cards[-1]
    assert card["header"]["title"]["content"] == "确认删除"
    buttons = []
    for element in card["body"]["elements"]:
        for column in element.get("columns", []):
            for child in column.get("elements", []):
                if child.get("tag") == "button":
                    buttons.append(child)
    assert buttons[0]["value"]["action"] == "delete_confirm"
    assert buttons[0]["value"]["source"] == "hermes_skill_card"


@pytest.mark.asyncio
async def test_skill_card_rejects_spoofed_plain_text_delete_confirm(tmp_path: Path):
    plugin, _ = _plugin_for_distillation(tmp_path)
    event = FakeChatEvent(umo="umo-card-spoof", is_group=True, sender_name="杨总")
    await plugin._record_conversation_discussion(
        event,
        "boss-1",
        "老板要看的结论应该是预算、周期和风险。",
    )
    await plugin._handle_conversation_distill_command(event, "/chat create-boss 杨总")

    payload = {
        "value": {
            "source": "hermes_skill_card",
            "action": "delete_confirm",
            "kind": "boss",
            "slug": "杨总",
        }
    }
    handled = await plugin._handle_conversation_distill_command(
        event,
        "__card_action__:" + json.dumps(payload, ensure_ascii=False),
    )

    assert handled is True
    assert (tmp_path / "bosses" / "杨总").exists()
    assert not (tmp_path / ".deleted").exists()


@pytest.mark.asyncio
async def test_skill_card_delete_confirm_requires_admin(tmp_path: Path):
    plugin, _ = _plugin_for_distillation(tmp_path)
    create_event = FakeChatEvent(
        umo="umo-card-admin-guard",
        is_group=True,
        sender_name="市场同事",
    )
    await plugin._record_conversation_discussion(
        create_event,
        "user-1",
        "同事执行会卡在人手和周期。",
    )
    await plugin._handle_conversation_distill_command(
        create_event,
        "/chat create-colleague 市场同事",
    )

    click_event = FakeChatEvent(
        umo="umo-card-admin-guard",
        is_group=True,
        sender_name="市场同事",
        is_admin=False,
    )
    click_event.is_card_action = True
    payload = {
        "value": {
            "source": "hermes_skill_card",
            "action": "delete_confirm",
            "kind": "colleague",
            "slug": "市场同事",
        }
    }
    handled = await plugin._handle_conversation_distill_command(
        click_event,
        "__card_action__:" + json.dumps(payload, ensure_ascii=False),
    )

    assert handled is True
    assert (tmp_path / "colleagues" / "市场同事").exists()
    assert "只有管理员" in str(click_event.sent[-1])


@pytest.mark.asyncio
async def test_skill_card_rollback_request_uses_latest_backup_by_timestamp(
    tmp_path: Path,
):
    plugin, _ = _plugin_for_distillation(tmp_path)
    event = FakeChatEvent(umo="umo-card-rollback-latest", is_group=True)
    skill_root = tmp_path / "bosses" / "yang-zong"
    versions = skill_root / "versions"
    versions.mkdir(parents=True)
    for version, timestamp in (
        ("v10", "2026-05-20T120000Z0000"),
        ("v2", "2026-05-21T120000Z0000"),
    ):
        backup = versions / f"{version}_{timestamp}"
        backup.mkdir()
        (backup / "SKILL.md").write_text(version, encoding="utf-8")
    sent_cards: list[dict[str, Any]] = []

    async def fake_send_skill_card(event_arg: Any, card: dict) -> str:
        sent_cards.append(card)
        return "mid-rollback-confirm"

    plugin._send_skill_card = fake_send_skill_card
    event.is_card_action = True
    payload = {
        "value": {
            "source": "hermes_skill_card",
            "action": "rollback_request",
            "kind": "boss",
            "slug": "yang-zong",
        }
    }
    handled = await plugin._handle_conversation_distill_command(
        event,
        "__card_action__:" + json.dumps(payload, ensure_ascii=False),
    )

    assert handled is True
    buttons = []
    for element in sent_cards[-1]["body"]["elements"]:
        for column in element.get("columns", []):
            for child in column.get("elements", []):
                if child.get("tag") == "button":
                    buttons.append(child)
    assert buttons[0]["value"]["action"] == "rollback_confirm"
    assert buttons[0]["value"]["version"] == "v2"


def _request(
    *,
    target_runtime: str = "claude_cli",
    job_id: str = "job-1",
) -> HermesTaskRequest:
    return HermesTaskRequest(
        router_decision={},
        user_input="Write a short plan",
        queue_job_id=job_id,
        payload={
            "target_runtime": target_runtime,
            "session_id": "session-1",
            "resource_key": "claude_cli_global",
        },
    )


def _bridge(
    *,
    gate: CapturingGate | None = None,
    sink: CapturingSink | None = None,
    estimated_run_seconds: float = 10,
) -> HermesBridge:
    return HermesBridge(
        quota_gate=gate or CapturingGate(),  # type: ignore[arg-type]
        callback_sink=sink or CapturingSink(),
        resource_configs={
            "claude_cli_global": ResourceConfig(
                key="claude_cli_global",
                estimated_run_seconds=estimated_run_seconds,  # type: ignore[arg-type]
            )
        },
    )


@pytest.mark.asyncio
async def test_submit_claude_cli_returns_job_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_exec(*args: str, **kwargs: Any) -> FakeProcess:
        return FakeProcess(
            returncode=0,
            stdout_lines=[
                json.dumps(
                    {
                        "type": "result",
                        "subtype": "success",
                        "result": "done",
                    }
                ).encode()
                + b"\n"
            ],
        )

    monkeypatch.setattr(
        "harness.hermes_bridge.asyncio.create_subprocess_exec", fake_exec
    )

    bridge = _bridge()
    job_id = await bridge.submit(_request(job_id="job-submit"))
    await bridge.drain()

    assert job_id == "job-submit"


@pytest.mark.asyncio
async def test_claude_cli_subprocess_args_and_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_exec(*args: str, **kwargs: Any) -> FakeProcess:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return FakeProcess(
            returncode=0,
            stdout_lines=[
                b'{"type":"result","subtype":"success","result":"ok"}\n',
            ],
        )

    monkeypatch.setenv("ANTHROPIC_API_KEY", "should-not-leak")
    monkeypatch.setattr(
        "harness.hermes_bridge.asyncio.create_subprocess_exec", fake_exec
    )

    bridge = _bridge()
    await bridge.submit(_request())
    await bridge.drain()

    args = captured["args"]
    assert args[0] == "claude"
    assert "-p" in args
    assert "Write a short plan" in args
    assert "--output-format" in args
    assert "stream-json" in args
    assert "--verbose" in args
    assert "ANTHROPIC_API_KEY" not in captured["kwargs"]["env"]
    assert captured["kwargs"]["env"]["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"


@pytest.mark.asyncio
async def test_claude_cli_success_emits_completed_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sink = CapturingSink()
    gate = CapturingGate()

    async def fake_exec(*args: str, **kwargs: Any) -> FakeProcess:
        return FakeProcess(
            returncode=0,
            stdout_lines=[
                b'{"type":"assistant","message":{"content":[{"type":"text","text":"working"}]}}\n',
                b'{"type":"result","subtype":"success","result":"final answer"}\n',
            ],
        )

    monkeypatch.setattr(
        "harness.hermes_bridge.asyncio.create_subprocess_exec", fake_exec
    )

    bridge = _bridge(gate=gate, sink=sink)
    await bridge.submit(_request(job_id="job-ok"))
    await bridge.drain()

    assert sink.payloads[-1].status == "completed"
    assert sink.payloads[-1].result is not None
    assert sink.payloads[-1].result["text"] == "final answer"
    assert gate.completed[-1]["job_id"] == "job-ok"


@pytest.mark.asyncio
async def test_claude_cli_returncode_failure_emits_failed_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sink = CapturingSink()
    gate = CapturingGate()

    async def fake_exec(*args: str, **kwargs: Any) -> FakeProcess:
        return FakeProcess(
            returncode=1,
            stderr_lines=[b"boom\n"],
        )

    monkeypatch.setattr(
        "harness.hermes_bridge.asyncio.create_subprocess_exec", fake_exec
    )

    bridge = _bridge(gate=gate, sink=sink)
    await bridge.submit(_request(job_id="job-fail"))
    await bridge.drain()

    assert sink.payloads[-1].status == "failed"
    assert sink.payloads[-1].error
    assert gate.failed[-1]["job_id"] == "job-fail"


@pytest.mark.asyncio
async def test_claude_cli_timeout_terminates_process_and_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sink = CapturingSink()
    gate = CapturingGate()
    slow_process = SlowProcess()

    async def fake_exec(*args: str, **kwargs: Any) -> SlowProcess:
        return slow_process

    monkeypatch.setattr(
        "harness.hermes_bridge.asyncio.create_subprocess_exec", fake_exec
    )

    bridge = _bridge(gate=gate, sink=sink, estimated_run_seconds=0.01)
    await bridge.submit(_request(job_id="job-timeout"))
    await bridge.drain()

    assert slow_process.terminate_called is True
    assert sink.payloads[-1].status == "failed"
    assert "timeout" in str(sink.payloads[-1].error).lower()
    assert "timeout" in gate.failed[-1]["error"].lower()


@pytest.mark.asyncio
async def test_unimplemented_runtime_raises_clear_error() -> None:
    bridge = _bridge()

    with pytest.raises(NotImplementedError, match="gemini_cli.*not implemented"):
        await bridge.submit(_request(target_runtime="gemini_cli"))


@pytest.mark.asyncio
async def test_short_intermediate_followed_by_final() -> None:
    task_id = "ta${REDACTED_API_KEY}"
    streamer = CapturingCardStreamer()
    plugin, context, engine = _plugin_with_card_streamer(streamer, task_id)

    short_intermediate = "已完成资料收集，正在整理结构化分析。"
    first_response = await plugin._handle_hermes_response(
        FakeHermesResponseRequest(
            {"task_id": task_id, "response": short_intermediate, "session_key": "s1"}
        )
    )

    assert first_response.status == 200
    assert json.loads(first_response.text)["via"] == "feishu_card"
    assert len(streamer.updated) == 1
    assert streamer.finalized == []
    assert task_id not in plugin._finalized_task_ids
    assert task_id in context.feishu_stream_map

    final_text = "最终方案\n" + ("这里是完整分析内容。" * 220)
    second_response = await plugin._handle_hermes_response(
        FakeHermesResponseRequest(
            {"task_id": task_id, "response": final_text, "session_key": "s1"}
        )
    )

    assert second_response.status == 200
    assert json.loads(second_response.text)["via"] == "feishu_card"
    assert len(streamer.finalized) == 1
    final_card_text = json.dumps(streamer.finalized[0]["card"], ensure_ascii=False)
    assert "最终方案" in final_card_text
    assert task_id in plugin._finalized_task_ids
    assert task_id not in context.feishu_stream_map
    assert engine.completed[-1]["task_id"] == task_id


@pytest.mark.asyncio
async def test_finalize_failure_does_not_dedup() -> None:
    task_id = "ta${REDACTED_API_KEY}"
    streamer = CapturingCardStreamer(fail_finalize=True)
    plugin, context, engine = _plugin_with_card_streamer(streamer, task_id)

    final_text = "最终方案\n" + ("这里是完整分析内容。" * 220)
    response = await plugin._handle_hermes_response(
        FakeHermesResponseRequest(
            {"task_id": task_id, "response": final_text, "session_key": "s1"}
        )
    )

    assert response.status == 200
    assert json.loads(response.text)["via"] == "feishu_card"
    assert task_id not in plugin._finalized_task_ids
    assert task_id in context.feishu_stream_map
    assert streamer.finalized == []
    assert engine.completed == []
