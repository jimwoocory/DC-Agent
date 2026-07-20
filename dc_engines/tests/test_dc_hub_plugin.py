from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_dc_hub_module():
    module_path = (
        Path(__file__).resolve().parents[2] / "data" / "plugins" / "dc_hub" / "main.py"
    )
    spec = importlib.util.spec_from_file_location("dc_hub_plugin_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _FakeContext:
    def __init__(self) -> None:
        self._star_manager = None
        self._stars = {}

    def register_web_api(self, *args, **kwargs):
        return None

    def get_registered_star(self, name: str):
        return self._stars.get(name)


class _FakeStar:
    def __init__(self, activated: bool = True) -> None:
        self.activated = activated


class _FakePluginManager:
    def __init__(self, context: _FakeContext) -> None:
        self.context = context
        self.turned_off = []
        self.turned_on = []
        self.uninstalled = []

    async def turn_off_plugin(self, plugin_id: str) -> None:
        self.turned_off.append(plugin_id)
        self.context._stars[plugin_id].activated = False

    async def turn_on_plugin(self, plugin_id: str) -> None:
        self.turned_on.append(plugin_id)
        self.context._stars[plugin_id].activated = True

    async def uninstall_plugin(
        self,
        plugin_id: str,
        *,
        delete_config: bool = False,
        delete_data: bool = False,
    ) -> None:
        self.uninstalled.append((plugin_id, delete_config, delete_data))


class _FakeEvent:
    def __init__(self, text: str) -> None:
        self.message_str = text
        self.result = None

    def set_result(self, result) -> None:
        self.result = result


def test_dc_hub_summary_groups_assistant_plugins(tmp_path: Path) -> None:
    module = _load_dc_hub_module()
    plugin = module.DCHubPlugin(
        _FakeContext(),
        {"state_path": str(tmp_path / "dc_hub_state.json")},
    )

    summary = plugin.summary()

    assert summary["plugin"] == "dc_hub"
    assert summary["version"] == "0.1.0"
    assert summary["total"] == 32
    assistant = next(c for c in summary["categories"] if c["id"] == "assistant_core")
    assistant_ids = {m["plugin_id"] for m in assistant["modules"]}
    assert {
        "dc_router",
        "concierge_plugin",
        "ai_inbox_plugin",
        "persona_factory",
    }.issubset(assistant_ids)
    feishu = next(c for c in summary["categories"] if c["id"] == "assistant_feishu")
    feishu_ids = {m["plugin_id"] for m in feishu["modules"]}
    assert "feishu_channel_control" in feishu_ids


def test_dc_hub_summary_does_not_count_unregistered_modules_as_enabled(
    tmp_path: Path,
) -> None:
    module = _load_dc_hub_module()
    plugin = module.DCHubPlugin(
        _FakeContext(),
        {"state_path": str(tmp_path / "dc_hub_state.json")},
    )

    summary = plugin.summary()
    assistant = next(c for c in summary["categories"] if c["id"] == "assistant_core")
    dc_router = next(m for m in assistant["modules"] if m["plugin_id"] == "dc_router")

    assert summary["enabled"] == 0
    assert assistant["enabled"] == 0
    assert summary["desired_enabled"] == summary["total"]
    assert dc_router["enabled"] is False
    assert dc_router["runtime_enabled"] is False
    assert dc_router["desired_enabled"] is True
    assert dc_router["registered"] is False
    assert dc_router["lifecycle"] in {"installed-unloaded", "missing"}


def test_dc_hub_summary_counts_only_registered_activated_modules(
    tmp_path: Path,
) -> None:
    module = _load_dc_hub_module()
    context = _FakeContext()
    context._stars["dc_router"] = _FakeStar(activated=True)
    context._stars["ai_inbox_plugin"] = _FakeStar(activated=False)
    plugin = module.DCHubPlugin(
        context,
        {"state_path": str(tmp_path / "dc_hub_state.json")},
    )

    summary = plugin.summary()
    assistant = next(c for c in summary["categories"] if c["id"] == "assistant_core")
    dc_router = next(m for m in assistant["modules"] if m["plugin_id"] == "dc_router")
    ai_inbox = next(
        m for m in assistant["modules"] if m["plugin_id"] == "ai_inbox_plugin"
    )

    assert summary["enabled"] == 1
    assert assistant["enabled"] == 1
    assert dc_router["enabled"] is True
    assert dc_router["runtime_enabled"] is True
    assert dc_router["desired_enabled"] is True
    assert ai_inbox["enabled"] is False
    assert ai_inbox["runtime_enabled"] is False
    assert ai_inbox["desired_enabled"] is True


async def test_dc_hub_command_turns_off_real_plugin_lifecycle(tmp_path: Path) -> None:
    module = _load_dc_hub_module()
    state_path = tmp_path / "dc_hub_state.json"
    context = _FakeContext()
    context._stars["chat_creator_plugin"] = _FakeStar(activated=True)
    manager = _FakePluginManager(context)
    context._star_manager = manager
    plugin = module.DCHubPlugin(context, {"state_path": str(state_path)})
    event = _FakeEvent("/dc-hub disable chat_creator_plugin")

    await plugin.dc_hub_command(event)

    assert event.result is not None
    assert "已停用插件" in event.result.get_plain_text()
    assert manager.turned_off == ["chat_creator_plugin"]
    assert plugin.is_enabled("chat_creator_plugin") is False
    assert state_path.exists()


async def test_dc_hub_refuses_uninstall_without_confirm(tmp_path: Path) -> None:
    module = _load_dc_hub_module()
    context = _FakeContext()
    context._stars["chat_creator_plugin"] = _FakeStar(activated=True)
    context._star_manager = _FakePluginManager(context)
    plugin = module.DCHubPlugin(
        context,
        {"state_path": str(tmp_path / "dc_hub_state.json")},
    )

    result = await plugin.lifecycle_action("uninstall", "chat_creator_plugin")

    assert result["status"] == "error"
    assert "卸载需要确认" in result["message"]


async def test_dc_hub_api_does_not_treat_false_string_as_confirm(
    tmp_path: Path,
) -> None:
    module = _load_dc_hub_module()
    context = _FakeContext()
    context._stars["chat_creator_plugin"] = _FakeStar(activated=True)
    manager = _FakePluginManager(context)
    context._star_manager = manager
    plugin = module.DCHubPlugin(
        context,
        {"state_path": str(tmp_path / "dc_hub_state.json")},
    )

    result = await plugin.lifecycle_action(
        "uninstall",
        "chat_creator_plugin",
        confirm=module._truthy("false"),
        delete_config=module._truthy("0"),
        delete_data=module._truthy("no"),
    )

    assert result["status"] == "error"
    assert manager.uninstalled == []
