"""Regression tests for ``routing.apply_decision`` CLI provider dispatch."""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_DC_AGENT_ROOT = Path(__file__).resolve().parents[2]
_PLUGINS_PARENT = _DC_AGENT_ROOT / "data" / "plugins"
if str(_DC_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_DC_AGENT_ROOT))
if str(_PLUGINS_PARENT) not in sys.path:
    sys.path.insert(0, str(_PLUGINS_PARENT))

try:
    importlib.import_module("astrbot")
except Exception:  # noqa: BLE001
    sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
try:
    importlib.import_module("astrbot.api")
except Exception:  # noqa: BLE001
    api_pkg = types.ModuleType("astrbot.api")
    sys.modules.setdefault("astrbot.api", api_pkg)
    sys.modules["astrbot"].api = api_pkg  # type: ignore[attr-defined]
sys.modules["astrbot.api"].logger = MagicMock()  # type: ignore[attr-defined]

dc_router_pkg = types.ModuleType("dc_router")
dc_router_pkg.__path__ = [str(_PLUGINS_PARENT / "dc_router")]  # type: ignore[attr-defined]
sys.modules["dc_router"] = dc_router_pkg

apply_decision_module = importlib.import_module("dc_router.routing.apply_decision")


@pytest.mark.asyncio
async def test_apply_decision_dispatches_cli_provider_and_annotates_event() -> None:
    context = MagicMock(name="context")
    event = MagicMock(name="event")
    event.get_platform_id.return_value = "巅池-Agent小助手"
    event.message_str = "#代码 看一下这个问题"
    extras: dict[str, str] = {}

    def _set_extra(key: str, value: object) -> None:
        extras[key] = str(value)

    event.set_extra.side_effect = _set_extra
    decision = types.SimpleNamespace(
        provider_id="cli/codex/gpt-5.5-medium",
        intent="simple_code",
        source="rules",
        reason="test cli path",
        metadata={"route": "cli"},
    )

    with patch(
        "dc_router.cli_handlers.dispatch_cli_provider",
        AsyncMock(return_value=True),
    ) as dispatch_cli:
        handled = await apply_decision_module.apply_decision(context, event, decision)

    assert handled is True
    dispatch_cli.assert_awaited_once_with(context, event, decision)
    assert extras["dc_router_provider"] == "cli/codex/gpt-5.5-medium"
    assert extras["dc_router_intent"] == "simple_code"
    assert extras["dc_router_source"] == "cli_rules"
    assert extras["dc_router_meta_route"] == "cli"
