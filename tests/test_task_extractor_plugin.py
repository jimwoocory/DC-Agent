from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


def _load_plugin_class():
    plugin_path = (
        Path(__file__).resolve().parents[1]
        / "data"
        / "plugins"
        / "task_extractor_plugin"
        / "main.py"
    )
    spec = importlib.util.spec_from_file_location(
        "task_extractor_plugin_main",
        plugin_path,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.TaskExtractorPlugin


class _PrivateEvent:
    message_str = (
        "蔡挺：我今天上午要整理五菱端午客户回访素材，下午还要给项目群同步进度。"
        "你帮我先列一个今天的工作优先级，并提醒我哪些内容适合沉淀到知识库。"
    )
    unified_msg_origin = "lark:FriendMessage:ou_user"
    is_at_or_wake_command = False
    message_obj = SimpleNamespace(type="PrivateMessage")

    def __init__(self) -> None:
        self.result = None

    def set_result(self, result) -> None:
        self.result = result

    def get_platform_id(self) -> str:
        return "lark"


@pytest.mark.asyncio
async def test_private_advisory_reminder_does_not_trigger_task_extraction() -> None:
    plugin_cls = _load_plugin_class()
    plugin = plugin_cls(
        SimpleNamespace(
            get_using_provider=lambda _umo: (_ for _ in ()).throw(
                AssertionError("provider should not be requested")
            ),
        )
    )
    event = _PrivateEvent()

    await plugin.on_message(event)

    assert event.result is None
