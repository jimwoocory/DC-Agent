from pathlib import Path


def test_diagnose_alert_channel_has_dc_engines_pythonpath() -> None:
    script = Path("scripts-watchdog/diagnose.sh").read_text(encoding="utf-8")

    assert "export PYTHONPATH=" in script
    assert "/Users/dianchi/DC-Agent/dc_engines" in script
    assert "-m dc_engines.alert_channel" in script


def test_diagnose_authorizes_codex_as_advanced_executor() -> None:
    script = Path("scripts-watchdog/diagnose.sh").read_text(encoding="utf-8")

    assert "-m dc_engines.codex_capability" in script
    assert "--capability incident_diagnosis" in script
    assert "--authorized-by deterministic_controller" in script
    assert "--owns-schedule" not in script


def test_diagnose_pins_openai_codex_runtime_and_repair_engine() -> None:
    script = Path("scripts-watchdog/diagnose.sh").read_text(encoding="utf-8")

    assert "--ignore-user-config" in script
    assert "--ephemeral" in script
    assert script.count("/opt/homebrew/bin/codex exec") == 1
    assert script.count("-C /private/tmp") == 1
    assert script.count("--disable apps") == 1
    assert script.count("--disable plugins") == 1
    assert script.count("--disable multi_agent") == 1
    assert "--sandbox read-only" in script
    assert "--model gpt-5.6-sol" in script
    assert 'model_provider="openai"' in script
    assert 'model_reasoning_effort="max"' in script
    assert "--output-schema" in script
    assert "bundle-schema" in script
    assert "split-bundle" in script
    assert "codex-bundle.json" in script
    assert "'codex_bundle': '$CODEX_BUNDLE'" in script
    assert "--action-url" in script
    assert "--action-label" in script
    assert "mark-review-notification" in script
    assert "repair_engine.py" in script
    assert "repair-proposal.json" in script
    assert "repair-result.json" in script
    assert "aihubmix" not in script.lower()


def test_watchdog_incident_ids_are_unique_per_service() -> None:
    script = Path("scripts-watchdog/dc-watchdog.sh").read_text(encoding="utf-8")

    assert script.count('incident_id="$(date +%s)-$name"') == 3
    assert "incident_id=$(date +%s)" not in script


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


def test_safe_restart_does_not_start_local_astrbot_in_nas_mode() -> None:
    script = Path("scripts-tools/safe_restart.sh").read_text(encoding="utf-8")

    assert 'PRIMARY_RUNTIME="${DC_AGENT_PRIMARY_RUNTIME:-nas}"' in script
    assert 'if [ "$PRIMARY_RUNTIME" = "nas" ]; then' in script
    assert "Local AstrBot restart skipped" in script


def test_watchdog_defers_restart_prone_service_alerts() -> None:
    script = Path("scripts-watchdog/dc-watchdog.sh").read_text(encoding="utf-8")

    assert "RESTART_GRACE_SEC=900" in script
    assert "restart_grace_deferred_diagnose" in script
    assert "restart_grace_skipped_diagnose" in script
    assert "restart_grace_expired" in script
