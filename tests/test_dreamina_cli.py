from __future__ import annotations

from types import SimpleNamespace

import pytest
from dc_engines.dreamina_cli import (
    dreamina_command_not_found_message,
    resolve_dreamina_executable,
)
from dc_engines.harness import ExecutorSettlement, HarnessEngine, HarnessTaskStore

from data.plugins.dreamina_plugin import main as dreamina

DreaminaPlugin = dreamina.DreaminaPlugin


def test_resolve_dreamina_executable_uses_path(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    executable = bin_dir / "dreamina"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)

    assert resolve_dreamina_executable({"PATH": str(bin_dir)}, home=tmp_path) == str(
        executable
    )


def test_resolve_dreamina_executable_falls_back_to_local_bin(tmp_path):
    local_bin = tmp_path / ".local" / "bin"
    local_bin.mkdir(parents=True)
    executable = local_bin / "dreamina"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)

    assert resolve_dreamina_executable({"PATH": ""}, home=tmp_path) == str(executable)


def test_resolve_dreamina_executable_honors_override(tmp_path):
    executable = tmp_path / "custom-dreamina"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)

    assert resolve_dreamina_executable(
        {"PATH": "", "DREAMINA_CLI_PATH": str(executable)}, home=tmp_path
    ) == str(executable)


def test_dreamina_command_not_found_message_names_override():
    assert "DREAMINA_CLI_PATH" in dreamina_command_not_found_message()


def test_dreamina_plugin_sanitizes_concurrency_failure() -> None:
    plugin = DreaminaPlugin(SimpleNamespace())
    output = (
        '{"submit_id":"video-123","gen_status":"fail",'
        '"fail_reason":"api error: ret=1310, message=ExceedConcurrencyLimit,'
        ' logid=internal-log"}'
    )

    ok, reason = plugin._check_gen_status(output)

    assert ok is False
    assert "并发已满" in reason
    assert "ExceedConcurrencyLimit" not in reason
    assert "logid" not in reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "method_name"), [("生成图片", "text2image"), ("生成视频", "text2video")]
)
async def test_menu_only_media_labels_do_not_trigger_legacy_commands(
    label: str,
    method_name: str,
) -> None:
    plugin = DreaminaPlugin(SimpleNamespace())
    event = SimpleNamespace(message_str=label)

    results = [item async for item in getattr(plugin, method_name)(event, "")]

    assert results == []


@pytest.mark.asyncio
async def test_cardless_dreamina_attempt_is_durable_and_reviewable(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = HarnessTaskStore(tmp_path / "harness.db")
    await store.initialize()
    engine = HarnessEngine(store)
    context = SimpleNamespace(
        harness_engine=engine,
        executor_settlement=ExecutorSettlement(engine),
    )
    plugin = DreaminaPlugin(context)
    event = SimpleNamespace(
        unified_msg_origin="webchat:FriendMessage:dreamina-test",
        message_str="用即梦生成一张蓝色圆形图片",
        message_obj=SimpleNamespace(
            raw_message=SimpleNamespace(message_id="message-dreamina-1")
        ),
        get_platform_id=lambda: "webchat",
        get_sender_id=lambda: "employee-1",
    )

    async def no_waiting_card(*_args, **_kwargs):
        return None

    monkeypatch.setattr(dreamina, "start_waiting_card_for_event", no_waiting_card)
    card = await plugin._start_waiting_card(
        event,
        title="即梦文生图",
        prompt="蓝色圆形",
        stage="Dreamina generating",
    )

    tasks = await store.list_tasks_for_session(event.unified_msg_origin)
    assert card is None
    assert len(tasks) == 1
    execution = await store.get_latest_execution_for_task(tasks[0].task_id)
    assert execution is not None
    assert execution.status == "running"

    output_path = tmp_path / "dreamina.png"
    output_path.write_bytes(b"png")
    finalized = await plugin._finish_waiting_card(
        card,
        title="即梦文生图",
        prompt="蓝色圆形",
        success=True,
        detail=str(output_path),
        output_uri=str(output_path),
    )

    task = await store.get_task(tasks[0].task_id)
    execution = await store.get_latest_execution_for_task(tasks[0].task_id)
    link = await store.get_task_link(tasks[0].task_id)
    artifact = await store.get_latest_artifact(
        scope_key=("media:webchat:webchat:FriendMessage:dreamina-test:employee-1"),
        artifact_kind="image",
    )
    assert finalized is False
    assert task is not None and task.status == "review_required"
    assert execution is not None and execution.status == "succeeded"
    assert link is not None and link.message_ref_id
    assert artifact is not None and artifact.uri == str(output_path)
