from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_launchd_installer_has_change_trigger_and_daily_reconcile() -> None:
    source = (
        ROOT / "scripts-tools" / "install-employee-auth-sync-launchd.sh"
    ).read_text(encoding="utf-8")

    assert '"StartInterval": 86400' in source
    assert '"WatchPaths"' in source
    assert "data/employees.db" in source
    assert "data/permissions.db" in source
    assert '"EnvironmentVariables"' in source
    assert "launchctl bootstrap" in source
    assert "launchctl bootout" in source


def test_scheduler_runner_loads_external_secrets_and_projects_both_databases() -> None:
    source = (ROOT / "scripts-tools" / "employee-auth-sync-runner.sh").read_text(
        encoding="utf-8"
    )

    assert ".dc-agent.env" in source
    assert "employee_auth_sync.env" in source
    assert "employee_auth_sync_secret" in source
    assert "employee_auth_sync.py" in source
    assert "DC_EMPLOYEE_DB_PATH" in source
    assert "DC_EMPLOYEE_PERMISSIONS_DB_PATH" in source
    assert "--permissions-db" in source
    assert "--reason" in source
