from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path


def _load_openclaw_module():
    module_path = Path("data/plugins/openclaw_on_demand/main.py")
    spec = importlib.util.spec_from_file_location(
        "openclaw_on_demand_main", module_path
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _plugin(module):
    plugin = module.OpenClawOnDemandPlugin.__new__(module.OpenClawOnDemandPlugin)
    plugin.openclaw_cwd = Path("/tmp/missing-openclaw")
    plugin.openclaw_port = 4312
    plugin.kick_port = 9120
    plugin.idle_timeout_seconds = 7200
    plugin.startup_wait_seconds = 0
    plugin.npm_cmd = ["npm", "run", "dev:ui"]
    plugin.log_dir = Path("/tmp/openclaw-test")
    plugin.openclaw_health_path = "/"
    plugin.openclaw_health_body_contains = "OpenClaw"
    plugin._openclaw_pid = None
    plugin._last_kick_at = 0.0
    plugin._lock = asyncio.Lock()
    plugin._runner = None
    plugin._idle_task = None
    return plugin


def test_openclaw_status_does_not_mark_wrong_service_ready(monkeypatch) -> None:
    module = _load_openclaw_module()
    plugin = _plugin(module)
    monkeypatch.setattr(plugin, "_port_in_use", lambda port: True)
    monkeypatch.setattr(plugin, "_openclaw_ready", lambda: False, raising=False)

    response = asyncio.run(plugin._handle_status(None))
    payload = json.loads(response.text)

    assert payload["openclaw_listening"] is True
    assert payload["openclaw_ready"] is False
    assert payload["availability"] == "wrong_service"


def test_openclaw_kick_does_not_redirect_to_wrong_service(monkeypatch) -> None:
    module = _load_openclaw_module()
    plugin = _plugin(module)
    monkeypatch.setattr(plugin, "_port_in_use", lambda port: True)
    monkeypatch.setattr(plugin, "_openclaw_ready", lambda: False, raising=False)

    response = asyncio.run(plugin._handle_kick(None))

    assert response.status == 503
    assert "服务不匹配" in response.text
