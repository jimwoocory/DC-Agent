#!/bin/bash
# Install or remove the DC-Agent employee usage audit cron job.

set -euo pipefail

DC_ROOT="/Users/dianchi/DC-Agent"
SCRIPT="$DC_ROOT/scripts-watchdog/employee_usage_audit.py"
MARKER="# DC-Agent employee usage audit (managed by scripts-tools/install-employee-usage-audit-cron.sh)"
CRON_LINE="30 9 * * * '$DC_ROOT/.venv/bin/python' '$SCRIPT' --days 2 --send --level info >> '$DC_ROOT/data/watchdog/usage_audit/cron.log' 2>&1"

usage() {
    echo "用法: $(basename "$0") install|remove|status"
    exit 1
}

[ $# -lt 1 ] && usage

case "$1" in
    install)
        mkdir -p "$DC_ROOT/data/watchdog/usage_audit"
        (
            crontab -l 2>/dev/null | grep -vE "DC-Agent employee usage audit|employee_usage_audit\.py" || true
            echo "$MARKER"
            echo "$CRON_LINE"
        ) | crontab -
        echo "✓ employee usage audit crontab 已安装"
        crontab -l | grep -A1 "DC-Agent employee usage audit"
        ;;
    remove)
        crontab -l 2>/dev/null | grep -vE "DC-Agent employee usage audit|employee_usage_audit\.py" | crontab -
        echo "✓ employee usage audit crontab 已移除"
        ;;
    status)
        if crontab -l 2>/dev/null | grep -q "DC-Agent employee usage audit"; then
            crontab -l | grep -A1 "DC-Agent employee usage audit"
        else
            echo "(未安装)"
        fi
        ;;
    *)
        usage
        ;;
esac
