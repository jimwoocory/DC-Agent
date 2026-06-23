#!/usr/bin/env bash
# Install / remove / status for the Feishu API sender cron job.
# Usage:
#   install-feishu-api-sender-cron.sh install   # add cron (default: every 6h)
#   install-feishu-api-sender-cron.sh remove    # remove cron
#   install-feishu-api-sender-cron.sh status    # show cron status + recent log

set -euo pipefail

DC_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$DC_ROOT/scripts-tools/feishu_api_sender.py"
LOG="$DC_ROOT/data/feishu_api_sender.log"
MARKER="# DC-Agent 飞书 API 定时发送 (managed by scripts-tools/install-feishu-api-sender-cron.sh)"
CRON_LINE="$MARKER\n0 */6 * * * cd $DC_ROOT && uv run python scripts-tools/feishu_api_sender.py --text \"巅池-Agent小助手 定时问候\" >> $LOG 2>&1"

case "${1:-}" in
  install)
    echo "Installing cron: every 6 hours"
    # Remove old entry first
    crontab -l 2>/dev/null | grep -vE "飞书 API 定时发送|feishu_api_sender\.py" | crontab - || true
    # Add new entry
    (crontab -l 2>/dev/null; echo -e "$CRON_LINE") | crontab -
    echo "Cron installed. Check with: crontab -l | grep feishu"
    ;;
  remove)
    echo "Removing cron..."
    crontab -l 2>/dev/null | grep -vE "飞书 API 定时发送|feishu_api_sender\.py" | crontab - || true
    echo "Cron removed."
    ;;
  status)
    echo "=== Cron entries ==="
    crontab -l 2>/dev/null | grep -A1 "飞书 API 定时发送" || echo "(none)"
    echo ""
    echo "=== Recent log (last 10 lines) ==="
    [ -f "$LOG" ] && tail -10 "$LOG" || echo "(no log yet)"
    ;;
  *)
    echo "Usage: $0 {install|remove|status}"
    exit 1
    ;;
esac
