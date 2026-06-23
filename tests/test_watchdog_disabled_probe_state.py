from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from harness.evaluator.kb_import_contract import load_contract, validate_contract

CONTRACT = Path("harness/contracts/watchdog_disabled_probe_state.json")
WATCHDOG_SCRIPT = Path("scripts-watchdog/dc-watchdog.sh")
WATCHDOGCTL = Path("scripts-watchdog/watchdogctl.py")
WATCHDOG_ENGINE = Path("scripts-watchdog/watchdog_engine.py")
WATCHDOG_STATE = Path("scripts-watchdog/watchdog_state.py")


def _load_watchdogctl_module():
    spec = importlib.util.spec_from_file_location("watchdogctl", WATCHDOGCTL)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_watchdog_state_module():
    spec = importlib.util.spec_from_file_location("watchdog_state", WATCHDOG_STATE)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_watchdog_engine_module():
    spec = importlib.util.spec_from_file_location("watchdog_engine", WATCHDOG_ENGINE)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_watchdog_disabled_probe_contract_is_valid() -> None:
    contract = load_contract(CONTRACT)

    assert validate_contract(contract) == []


def test_dc_watchdog_marks_disabled_heartbeat_probes() -> None:
    source = WATCHDOG_SCRIPT.read_text(encoding="utf-8")
    engine = _load_watchdog_engine_module()

    disabled = {probe.name: probe.reason for probe in engine.DISABLED_PROBES}
    assert disabled["nas_watchdog_heartbeat"].startswith("NAS/Feishu sync jobs paused")
    assert disabled["feishu_sync_heartbeat"].startswith("NAS/Feishu sync jobs paused")
    assert 'watchdog_engine.py" list-disabled' in source
    assert "state_set_disabled" in source
    assert "watchdog_state.py" in source
    assert "disabled_reason" in WATCHDOG_STATE.read_text(encoding="utf-8")


def test_watchdogctl_uses_active_service_entries_for_probe_enabled() -> None:
    module = _load_watchdogctl_module()

    assert module.probe_enabled("astrbot_api") is True
    assert module.probe_enabled("feishu_sync_heartbeat") is False


def test_watchdogctl_ignores_probe_names_in_service_comments(tmp_path) -> None:
    module = _load_watchdogctl_module()
    script = tmp_path / "scripts-watchdog" / "watchdog_engine.py"
    script.parent.mkdir()
    script.write_text(
        """
from __future__ import annotations

class Probe:
    def __init__(self, name, groups=()):
        self.name = name
        self.groups = groups

ACTIVE_PROBES = (Probe("astrbot_api", ("watchdog",)),)
DISABLED_PROBES = (Probe("feishu_sync_heartbeat", ("watchdog",)),)

def probe_enabled(name):
    return name == "astrbot_api"
""",
        encoding="utf-8",
    )
    module.DC_ROOT = tmp_path

    assert module.probe_enabled("astrbot_api") is True
    assert module.probe_enabled("feishu_sync_heartbeat") is False


def test_disabled_probe_state_preserves_existing_disabled_since_and_metadata() -> None:
    module = _load_watchdog_state_module()
    state = {
        "feishu_sync_heartbeat": {
            "status": "disabled",
            "since": "2026-06-04T00:00:00Z",
            "disabled_reason": "old reason",
            "last_diag_ts": 123,
            "operator_note": "paused by owner",
        }
    }

    module.mark_disabled_probe_state(
        state,
        "feishu_sync_heartbeat",
        reason="NAS/Feishu sync jobs paused",
        since="2026-06-05T00:00:00Z",
    )

    entry = state["feishu_sync_heartbeat"]
    assert entry["status"] == "disabled"
    assert entry["since"] == "2026-06-04T00:00:00Z"
    assert entry["disabled_reason"] == "NAS/Feishu sync jobs paused"
    assert entry["last_diag_ts"] == 123
    assert entry["operator_note"] == "paused by owner"


def test_disabled_probe_state_replaces_old_stale_failure() -> None:
    module = _load_watchdog_state_module()
    state = {
        "feishu_sync_heartbeat": {
            "status": "fail:stale_4020s",
            "since": "2026-06-02T04:18:02Z",
            "last_diag_ts": 123,
        }
    }

    module.mark_disabled_probe_state(
        state,
        "feishu_sync_heartbeat",
        reason="NAS/Feishu sync jobs paused",
        since="2026-06-05T00:00:00Z",
    )

    entry = state["feishu_sync_heartbeat"]
    assert entry["status"] == "disabled"
    assert entry["since"] == "2026-06-05T00:00:00Z"
    assert entry["disabled_reason"] == "NAS/Feishu sync jobs paused"
    assert entry["last_diag_ts"] == 123


def test_disabled_probe_state_shape_matches_contract() -> None:
    state = {
        "feishu_sync_heartbeat": {
            "status": "disabled",
            "since": "2026-06-05T00:00:00Z",
            "disabled_reason": "NAS/Feishu sync jobs paused",
        }
    }
    text = json.dumps(state)

    assert '"status": "disabled"' in text
    assert "fail:stale" not in text
