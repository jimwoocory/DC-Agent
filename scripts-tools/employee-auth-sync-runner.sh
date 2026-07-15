#!/bin/zsh
set -euo pipefail

DC_ROOT="${DC_ROOT:-/Users/dianchi/DC-Agent}"
ENV_FILE="${DC_AGENT_ENV_FILE:-$HOME/.dc-agent.env}"
SYNC_ENV_FILE="${DC_EMPLOYEE_AUTH_SYNC_ENV_FILE:-$HOME/.config/dc-agent/employee_auth_sync.env}"
SECRET_FILE="${DC_EMPLOYEE_AUTH_SYNC_SECRET_FILE:-$HOME/.config/dc-agent/employee_auth_sync_secret}"
PYTHON_BIN="${DC_AGENT_PYTHON:-$DC_ROOT/.venv/bin/python}"
REASON="${1:-scheduled_reconcile}"
DB_PATH="${DC_EMPLOYEE_DB_PATH:-$DC_ROOT/data/employees.db}"
PERMISSIONS_DB_PATH="${DC_EMPLOYEE_PERMISSIONS_DB_PATH:-$DC_ROOT/data/permissions.db}"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  source "$ENV_FILE"
  set +a
fi

if [[ -f "$SYNC_ENV_FILE" ]]; then
  set -a
  source "$SYNC_ENV_FILE"
  set +a
fi

if [[ -z "${DESKTOP_EMPLOYEE_SYNC_SECRET:-}" && -r "$SECRET_FILE" ]]; then
  DESKTOP_EMPLOYEE_SYNC_SECRET="$(tr -d '\r\n' < "$SECRET_FILE")"
  export DESKTOP_EMPLOYEE_SYNC_SECRET
fi

exec "$PYTHON_BIN" "$DC_ROOT/scripts-tools/employee_auth_sync.py" \
  --db "$DB_PATH" \
  --permissions-db "$PERMISSIONS_DB_PATH" \
  --reason "$REASON"
