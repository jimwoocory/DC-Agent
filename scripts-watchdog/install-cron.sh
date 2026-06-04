#!/bin/bash
# 装/卸载 DC-Agent watchdog crontab
# 用法：
#   install-cron.sh install   装
#   install-cron.sh remove    卸
#   install-cron.sh status    看当前 crontab 有没装

set -euo pipefail

DC_ROOT="/Users/dianchi/DC-Agent"
WD_SCRIPT="$DC_ROOT/scripts-watchdog/dc-watchdog.sh"
CRON_LINE="* * * * * '$WD_SCRIPT' >> '$DC_ROOT/data/watchdog/cron.log' 2>&1"
MARKER="# DC-Agent watchdog (managed by scripts-watchdog/install-cron.sh)"

cmd="${1:-status}"

case "$cmd" in
    install)
        # 取现有 crontab，删旧 marker block，加新 block
        (
            crontab -l 2>/dev/null | grep -vE "DC-Agent watchdog|scripts-watchdog/dc-watchdog\.sh" || true
            echo "$MARKER"
            echo "$CRON_LINE"
        ) | crontab -
        echo "✓ crontab 已装。验证："
        crontab -l | grep -A1 "DC-Agent watchdog"
        ;;
    remove)
        crontab -l 2>/dev/null | grep -vE "DC-Agent watchdog|scripts-watchdog/dc-watchdog\.sh" | crontab -
        echo "✓ crontab 已卸"
        ;;
    status)
        echo "=== 当前 crontab DC-Agent watchdog entry ==="
        if crontab -l 2>/dev/null | grep -q "DC-Agent watchdog"; then
            crontab -l | grep -A1 "DC-Agent watchdog"
            echo ""
            echo "=== 最近 watchdog 日志 ==="
            tail -10 "$DC_ROOT/data/watchdog/cron.log" 2>/dev/null || echo "(还没产生日志)"
        else
            echo "(未装)"
        fi
        ;;
    *)
        echo "用法: $0 {install|remove|status}"
        exit 1
        ;;
esac
