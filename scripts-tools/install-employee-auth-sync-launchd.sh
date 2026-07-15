#!/bin/zsh
set -euo pipefail

DC_ROOT="${DC_ROOT:-/Users/dianchi/DC-Agent}"
LABEL="com.dcagent.employee-auth-sync"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
RUNNER="$DC_ROOT/scripts-tools/employee-auth-sync-runner.sh"
LOG_DIR="$DC_ROOT/data/watchdog/employee_auth_sync"
DB_PATH="${DC_EMPLOYEE_DB_PATH:-$DC_ROOT/data/employees.db}"
PERMISSIONS_DB_PATH="${DC_EMPLOYEE_PERMISSIONS_DB_PATH:-$DC_ROOT/data/permissions.db}"
SYNC_ENV_FILE="${DC_EMPLOYEE_AUTH_SYNC_ENV_FILE:-$HOME/.config/dc-agent/employee_auth_sync.env}"
SECRET_FILE="${DC_EMPLOYEE_AUTH_SYNC_SECRET_FILE:-$HOME/.config/dc-agent/employee_auth_sync_secret}"
DOMAIN="gui/$UID"
ACTION="${1:-status}"

write_plist() {
  mkdir -p "${PLIST:h}" "$LOG_DIR"
  /usr/bin/python3 - \
    "$PLIST" "$LABEL" "$RUNNER" "$DC_ROOT" "$LOG_DIR" \
    "$DB_PATH" "$PERMISSIONS_DB_PATH" "$SYNC_ENV_FILE" "$SECRET_FILE" <<'PY'
import plistlib
import sys
from pathlib import Path

(
    path,
    label,
    runner,
    root,
    log_dir,
    db_path,
    permissions_db_path,
    sync_env_file,
    secret_file,
) = sys.argv[1:]
payload = {
    "Label": label,
    "ProgramArguments": [runner, "scheduled_reconcile"],
    "WorkingDirectory": root,
    "RunAtLoad": True,
    "StartInterval": 86400,
    "WatchPaths": [db_path, permissions_db_path],
    "EnvironmentVariables": {
        "DC_EMPLOYEE_DB_PATH": db_path,
        "DC_EMPLOYEE_PERMISSIONS_DB_PATH": permissions_db_path,
        "DC_EMPLOYEE_AUTH_SYNC_ENV_FILE": sync_env_file,
        "DC_EMPLOYEE_AUTH_SYNC_SECRET_FILE": secret_file,
    },
    "ProcessType": "Background",
    "ThrottleInterval": 30,
    "StandardOutPath": str(Path(log_dir) / "launchd.log"),
    "StandardErrorPath": str(Path(log_dir) / "launchd.error.log"),
}
with Path(path).open("wb") as handle:
    plistlib.dump(payload, handle, sort_keys=True)
PY
  chmod 644 "$PLIST"
}

case "$ACTION" in
  install)
    write_plist
    launchctl bootout "$DOMAIN" "$PLIST" >/dev/null 2>&1 || true
    launchctl bootstrap "$DOMAIN" "$PLIST"
    launchctl enable "$DOMAIN/$LABEL"
    launchctl kickstart -k "$DOMAIN/$LABEL"
    echo "employee authorization sync launchd job installed"
    ;;
  remove)
    launchctl bootout "$DOMAIN" "$PLIST" >/dev/null 2>&1 || true
    rm -f "$PLIST"
    echo "employee authorization sync launchd job removed"
    ;;
  status)
    if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
      launchctl print "$DOMAIN/$LABEL"
    else
      echo "employee authorization sync launchd job is not loaded"
      exit 1
    fi
    ;;
  *)
    echo "usage: $0 {install|remove|status}" >&2
    exit 2
    ;;
esac
