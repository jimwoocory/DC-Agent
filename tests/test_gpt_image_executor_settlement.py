from __future__ import annotations

from types import SimpleNamespace

import pytest
from dc_engines.harness import ExecutorSettlement, HarnessEngine, HarnessTaskStore

from data.plugins.gpt_image_plugin import main as gpt_image


class _ConversationManager:
    async def get_curr_conversation_id(self, _session_id: str) -> str:
        return "conversation-1"


class _Event:
    def __init__(self) -> None:
        self.unified_msg_origin = "webchat:FriendMessage:test"
        self.message_str = "帮我生成一张蓝色圆形图片"
        self.message_obj = SimpleNamespace(
            raw_message=SimpleNamespace(message_id="message-1")
        )
        self._extras: dict[str, object] = {}

    def get_extra(self, key: str):
        return self._extras.get(key)

    def set_extra(self, key: str, value: object) -> None:
        self._extras[key] = value

    def get_platform_id(self) -> str:
        return "webchat"

    def get_sender_id(self) -> str:
        return "employee-1"

    def plain_result(self, text: str):
        return ("plain", text)

    def image_result(self, path: str):
        return ("image", path)


class _RecordingSettlement:
    def __init__(self, delegate: ExecutorSettlement) -> None:
        self.delegate = delegate
        self.begin_called = False

    async def begin(self, **kwargs):
        self.begin_called = True
        return await self.delegate.begin(**kwargs)

    async def settle(self, **kwargs):
        return await self.delegate.settle(**kwargs)


@pytest.mark.asyncio
async def test_direct_gpt_image_tool_begins_and_settles_before_provider_work(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = HarnessTaskStore(tmp_path / "harness.db")
    await store.initialize()
    engine = HarnessEngine(store)
    settlement = _RecordingSettlement(ExecutorSettlement(engine))
    context = SimpleNamespace(
        harness_engine=engine,
        executor_settlement=settlement,
        conversation_manager=_ConversationManager(),
    )
    plugin = gpt_image.GPTImagePlugin(context)
    event = _Event()
    output_path = tmp_path / "generated.png"
    output_path.write_bytes(b"png")

    async def no_waiting_card(*_args, **_kwargs):
        return None

    async def no_final_card(*_args, **_kwargs):
        return False

    def fake_provider(*_args, **_kwargs):
        assert settlement.begin_called is True
        return True, str(output_path)

    monkeypatch.setattr(plugin, "_start_image_waiting_card", no_waiting_card)
    monkeypatch.setattr(plugin, "_finish_image_waiting_card", no_final_card)
    monkeypatch.setattr(gpt_image, "_call_codex_image_gen", fake_provider)

    results = [
        item
        async for item in plugin.tool_generate_image(
            event,
            "蓝色圆形",
            quality="low",
            aspect_ratio="square",
        )
    ]

    tasks = await store.list_tasks_for_session(event.unified_msg_origin)
    assert len(tasks) == 1
    task = tasks[0]
    execution = await store.get_latest_execution_for_task(task.task_id)
    link = await store.get_task_link(task.task_id)
    assert execution is not None
    assert execution.executor_kind == "media"
    assert execution.status == "succeeded"
    assert task.status == "review_required"
    assert link is not None
    assert link.message_ref_id
    artifact = await store.get_latest_artifact(
        scope_key="media:webchat:conversation-1:employee-1",
        artifact_kind="image",
    )
    assert artifact is not None
    assert artifact.uri == str(output_path)
    assert artifact.version == 1
    assert ("image", str(output_path)) in results
