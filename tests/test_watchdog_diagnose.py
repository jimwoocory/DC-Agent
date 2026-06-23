from pathlib import Path


def test_diagnose_alert_channel_has_dc_engines_pythonpath() -> None:
    script = Path("scripts-watchdog/diagnose.sh").read_text(encoding="utf-8")

    assert "export PYTHONPATH=" in script
    assert "/Users/dianchi/DC-Agent/dc_engines" in script
    assert "-m dc_engines.alert_channel" in script


def test_watchdog_maintenance_detection_ignores_codex_app_server() -> None:
    engine = Path("scripts-watchdog/watchdog_engine.py").read_text(encoding="utf-8")

    assert "is_workspace_agent = root_text in command" in engine
    assert '"/Applications/Codex.app/" in command' not in engine


def test_safe_restart_keeps_watchdog_quiet_after_health_recovers() -> None:
    script = Path("scripts-tools/safe_restart.sh").read_text(encoding="utf-8")

    assert "SAFE_RESTART_WATCHDOG_QUIET_SECONDS" in script
    assert "WATCHDOG_QUIET_SECONDS" in script
    assert 'sleep "$WATCHDOG_QUIET_SECONDS"' in script


def test_safe_restart_astrbot_falls_back_to_current_start_method() -> None:
    script = Path("scripts-tools/safe_restart.sh").read_text(encoding="utf-8")

    assert "restart_astrbot_without_launchd" in script
    assert "ASTRBOT_TMUX_SESSION" in script
    assert 'tmux new-session -d -s "$ASTRBOT_TMUX_SESSION"' in script
    assert "./start-all.sh" in script
    assert "pgrep -f" in script
    assert 'POST_HEALTH_CMD="$DC_ROOT/scripts-tools/card-system-health.py"' in script


def test_watchdog_defers_restart_prone_service_alerts() -> None:
    script = Path("scripts-watchdog/dc-watchdog.sh").read_text(encoding="utf-8")

    assert "RESTART_GRACE_SEC=900" in script
    assert "restart_grace_deferred_diagnose" in script
    assert "restart_grace_skipped_diagnose" in script
    assert "restart_grace_expired" in script
