from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_chat_creator_plugin_module():
    module_path = (
        Path(__file__).resolve().parents[2]
        / "data"
        / "plugins"
        / "chat_creator_plugin"
        / "main.py"
    )
    spec = importlib.util.spec_from_file_location(
        "dc_chat_creator_plugin_test",
        module_path,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _FakeContext:
    def get_config(self):
        return {}


class _FakePrivateEvent:
    def __init__(self, text: str) -> None:
        self.message_str = text
        self.result = None

    def set_result(self, result) -> None:
        self.result = result


async def test_group_help_request_replies_with_join_and_create_guidance() -> None:
    module = _load_chat_creator_plugin_module()
    plugin = module.ChatCreatorPlugin(_FakeContext())
    event = _FakePrivateEvent("怎么拉你进群")

    await plugin.group_help_request(event)

    assert event.result is not None
    text = event.result.get_plain_text()
    assert "添加机器人「巅池-Agent小助手」" in text
    assert "/chat new" in text
    assert "/chat invite" in text
