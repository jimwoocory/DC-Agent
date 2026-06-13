from scripts.check_clean import violation_for, visible_status_paths


def test_runtime_workspace_and_nas_outputs_are_forbidden() -> None:
    assert violation_for("data/workspaces/local/doc.md") == "runtime output directory"
    assert violation_for("nas/knowledge/chat.jsonl") == "runtime output directory"
    assert (
        violation_for("data/feishu-rpa-persistent-profile/Default/Cookies")
        == "runtime output directory"
    )
    assert violation_for(".qoder/cache/session.json") == "runtime output directory"


def test_runtime_event_and_state_files_are_forbidden() -> None:
    assert (
        violation_for("data/antigravity_health_events.jsonl")
        == "runtime event/state file"
    )
    assert violation_for("data/grok_worker_state.json") == "runtime event/state file"
    assert (
        violation_for("nas_sync/sync_latest.failstate") == "runtime event/state file"
    )
    assert violation_for("nas_sync/watchdog.log") == "runtime event/state file"
    assert violation_for("nas_sync/state.json") == "runtime event/state file"
    assert violation_for("nas_sync/.cache/index") == "runtime event/state file"
    assert violation_for("nas_sync/sync_mtime_cache") == "runtime event/state file"
    assert (
        violation_for("h_send_daily_cron_state.json") == "runtime event/state file"
    )


def test_sensitive_config_changes_are_only_status_or_staged_violations() -> None:
    assert violation_for("data/config/hermes_bridge_config.json") is None
    assert (
        violation_for("data/config/hermes_bridge_config.json", source="status") is None
    )
    assert (
        violation_for("data/config/hermes_bridge_config.json", source="staged")
        == "sensitive local config change"
    )


def test_config_templates_are_allowed_in_status() -> None:
    assert (
        violation_for("data/config/example_config.example.json", source="status")
        is None
    )
    assert (
        violation_for("data/config/example_config.example.yaml", source="staged")
        is None
    )
    assert (
        violation_for("data/config/example_config.example.yml", source="staged")
        is None
    )


def test_reviewed_live_configs_are_allowed_when_staged() -> None:
    assert (
        violation_for("data/config/openclaw_on_demand_config.json", source="staged")
        is None
    )
    assert (
        violation_for("data/config/system_entries_config.json", source="staged")
        is None
    )
    assert violation_for("data/config/knowledge_cycle.env", source="staged") is None
    assert (
        violation_for("data/config/system_entries_config.json", source="status") is None
    )


def test_sensitive_config_guard_requires_template_suffix() -> None:
    sensitive_paths = [
        "data/config/example_credentials.json",
        "data/config/template_secret.yaml",
        "data/config/local.env",
        "data/config/access.txt",
        "data/config/session.token",
        "data/config/app.secret",
        "data/config/private.key",
    ]
    for path in sensitive_paths:
        assert (
            violation_for(path, source="staged")
            == "sensitive local config change"
        )
    assert violation_for("data/config/template_secret.yaml", source="status") is None
    assert (
        violation_for("data/config/example_config.template.json", source="staged")
        == "sensitive local config change"
    )


def test_visible_status_paths_preserve_first_path_character(monkeypatch) -> None:
    monkeypatch.setattr(
        "scripts.check_clean.git_status_lines",
        lambda: [" M .gitignore", " M astrbot/core/example.py"],
    )

    assert visible_status_paths() == [".gitignore", "astrbot/core/example.py"]
