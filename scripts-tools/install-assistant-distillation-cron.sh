#!/bin/bash
# Install or remove the DC-Agent assistant distillation worker cron job.

set -euo pipefail

DC_ROOT="/Users/dianchi/DC-Agent"
SCRIPT="$DC_ROOT/scripts-watchdog/assistant_distillation_worker.py"
LOG_DIR="$DC_ROOT/data/watchdog/assistant_distillation"
MARKER="# DC-Agent assistant distillation worker (managed by scripts-tools/install-assistant-distillation-cron.sh)"
CRON_LINE="*/30 * * * * '$DC_ROOT/.venv/bin/python' '$SCRIPT' --once >> '$LOG_DIR/cron.log' 2>&1"

usage() {
    echo "用法: $(basename "$0") install|remove|status"
    exit 1
}

[ $# -lt 1 ] && usage

case "$1" in
    install)
        mkdir -p "$LOG_DIR"
        (
            crontab -l 2>/dev/null | grep -vE "DC-Agent assistant distillation worker|assistant_distillation_worker\.py" || true
            echo "$MARKER"
            echo "$CRON_LINE"
        ) | crontab -
        echo "✓ assistant distillation worker crontab 已安装"
        crontab -l | grep -A1 "DC-Agent assistant distillation worker"
        ;;
    remove)
        crontab -l 2>/dev/null | grep -vE "DC-Agent assistant distillation worker|assistant_distillation_worker\.py" | crontab -
        echo "✓ assistant distillation worker crontab 已移除"
        ;;
    status)
        if crontab -l 2>/dev/null | grep -q "DC-Agent assistant distillation worker"; then
            crontab -l | grep -A1 "DC-Agent assistant distillation worker"
        else
            echo "(未安装)"
        fi
        ;;
    *)
        usage
        ;;
esac
