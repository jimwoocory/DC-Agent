from pathlib import Path

DC_ROOT = Path(__file__).resolve().parents[1]


def test_openclaw_on_demand_plugin_is_retired() -> None:
    assert not (DC_ROOT / "data/plugins/openclaw_on_demand").exists()


def test_runtime_control_files_have_no_legacy_claw_coupling() -> None:
    blocked_tokens = (
        "openclaw_watchdog",
        "openclaw-token-monitor",
        "com.dcagent.openclaw-watchdog",
        "localhost:4312",
        "127.0.0.1:9120",
        "/Users/dianchi/Openclaw",
    )
    runtime_files = (
        "data/plugins/system_entries/main.py",
        "data/plugins/system_entries/_conf_schema.json",
        "data/plugins/system_entries/dc-dashboard-quick-entries.js",
        "data/plugins/dc_hub/main.py",
        "scripts-watchdog/watchdog_engine.py",
        "scripts-watchdog/watchdogctl.py",
        "scripts-watchdog/repair_engine.py",
        "scripts-watchdog/dc-watchdog.sh",
        "scripts-tools/safe_restart.sh",
        "scripts-tools/verify_llm_after_restart.sh",
        "scripts-tools/cmd_config_unredact_watchdog.sh",
    )

    for relative_path in runtime_files:
        content = (DC_ROOT / relative_path).read_text(encoding="utf-8")
        for token in blocked_tokens:
            assert token not in content, f"{relative_path} still contains {token}"
