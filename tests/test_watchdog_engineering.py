from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from harness.evaluator.kb_import_contract import load_contract, validate_contract

CONTRACT = Path("harness/contracts/watchdog_engineering.json")
WATCHDOG_SCRIPT = Path("scripts-watchdog/dc-watchdog.sh")
WATCHDOG_ENGINE = Path("scripts-watchdog/watchdog_engine.py")
WATCHDOGCTL = Path("scripts-watchdog/watchdogctl.py")


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_watchdog_engineering_contract_is_valid() -> None:
    contract = load_contract(CONTRACT)

    assert validate_contract(contract) == []


def test_dc_watchdog_consumes_engine_probe_registry() -> None:
    source = WATCHDOG_SCRIPT.read_text(encoding="utf-8")

    assert 'watchdog_engine.py" list-active' in source
    assert 'watchdog_engine.py" list-disabled' in source
    assert '"astrbot_api|http|http://127.0.0.1:6185/api/stat/start-time"' not in source
    assert '"knowledge_cycle|knowledge_cycle|cron_tick"' not in source


def test_engine_exposes_expected_active_and_disabled_probe_names() -> None:
    engine = _load_module(WATCHDOG_ENGINE, "watchdog_engine")

    assert "astrbot_api" in engine.active_probe_names()
    assert "knowledge_cycle" in engine.active_probe_names()
    assert "feishu_sync_heartbeat" in engine.disabled_probe_names()
    assert engine.probe_enabled("astrbot_api") is True
    assert engine.probe_enabled("feishu_sync_heartbeat") is False


def test_nas_runtime_monitors_remote_assistant_without_local_astrbot() -> None:
    engine = _load_module(WATCHDOG_ENGINE, "watchdog_engine_nas")

    active = engine.active_probe_names("nas")
    disabled = engine.disabled_probe_names("nas")

    assert "nas_assistant_chat_health" in active
    assert "astrbot_dashboard" not in active
    assert "astrbot_api" not in active
    assert "assistant_chat_health" not in active
    assert "astrbot_dashboard" in disabled
    assert "astrbot_api" in disabled
    assert "hermes_gateway" in active


def test_nas_chat_health_uses_restart_grace_for_transient_502() -> None:
    engine = _load_module(WATCHDOG_ENGINE, "watchdog_engine_nas_restart_grace")

    assert engine.should_suppress_for_agent_maintenance("nas_assistant_chat_health")


def test_dc_watchdog_defaults_to_nas_runtime_ownership() -> None:
    source = WATCHDOG_SCRIPT.read_text(encoding="utf-8")

    assert 'PRIMARY_RUNTIME="${DC_AGENT_PRIMARY_RUNTIME:-nas}"' in source
    assert 'list-active --runtime "$PRIMARY_RUNTIME"' in source
    assert 'list-disabled --runtime "$PRIMARY_RUNTIME"' in source


def test_dashboard_static_probe_requires_index_html(tmp_path) -> None:
    engine = _load_module(WATCHDOG_ENGINE, "watchdog_engine")
    dist = tmp_path / "dist"
    dist.mkdir()

    assert engine.dashboard_static_assets_ready(dist) is False

    (dist / "index.html").write_text("<!doctype html>", encoding="utf-8")

    assert engine.dashboard_static_assets_ready(dist) is True


def test_engine_agent_maintenance_ignores_long_lived_codex_app_server() -> None:
    engine = _load_module(WATCHDOG_ENGINE, "watchdog_engine")
    rows = [
        "849 04-06:00:20 /Applications/Codex.app/Contents/Resources/codex app-server --analytics-default-enabled",
    ]

    assert (
        engine.agent_maintenance_reason_from_rows(
            rows,
            grace_seconds=900,
            dc_root="/Users/dianchi/DC-Agent",
        )
        is None
    )


def test_engine_agent_maintenance_detects_recent_cli_or_workspace_agent() -> None:
    engine = _load_module(WATCHDOG_ENGINE, "watchdog_engine")
    rows = [
        "100 00:03 codex exec --help",
        "101 04-06:00:20 claude -p /Users/dianchi/DC-Agent fix watchdog",
    ]

    reason = engine.agent_maintenance_reason_from_rows(
        rows,
        grace_seconds=900,
        dc_root="/Users/dianchi/DC-Agent",
    )

    assert reason is not None
    assert "100:codex exec" in reason
    assert "101:claude -p /Users/dianchi/DC-Agent" in reason


def test_watchdogctl_uses_engine_registry() -> None:
    module = _load_module(WATCHDOGCTL, "watchdogctl")

    assert module.probe_enabled("astrbot_api") is True
    assert module.probe_enabled("feishu_sync_heartbeat") is False
    groups = module.watchdog_probe_groups()
    assert groups["astrbot_api"]
    assert groups["feishu_sync_heartbeat"]
