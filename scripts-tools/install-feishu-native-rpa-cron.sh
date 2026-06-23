#!/bin/bash
# 装/卸载「飞书原生 RPA 定时发送」cron
# 用法：
#   install-feishu-native-rpa-cron.sh install   装 cron
#   install-feishu-native-rpa-cron.sh remove    卸
#   install-feishu-native-rpa-cron.sh status    看当前状态 + 最近日志

set -euo pipefail

DC_ROOT="/Users/dianchi/DC-Agent"
SCRIPT="$DC_ROOT/scripts-tools/feishu_native_rpa_sender.py"
PYTHON="$DC_ROOT/.venv/bin/python"
LOG="$DC_ROOT/data/feishu_native_rpa.log"
MARKER="# DC-Agent 飞书原生 RPA 定时发送 (managed by scripts-tools/install-feishu-native-rpa-cron.sh)"

# 默认话术 + 目标。按需改这两行。
SEND_TEXT="小助手，帮我记一下这条消息。"
SEND_TARGET="巅池-Agent小助手"

# 每 6 小时跑一次（0:00, 6:00, 12:00, 18:00）。频率按需调整。
CRON_LINE="0 */6 * * * cd '$DC_ROOT' && '$PYTHON' '$SCRIPT' --text '$SEND_TEXT' --target '$SEND_TARGET' >> '$LOG' 2>&1"

cmd="${1:-status}"

case "$cmd" in
    install)
        (
            crontab -l 2>/dev/null | grep -vE "飞书原生 RPA 定时发送|feishu_native_rpa_sender\.py" || true
            echo "$MARKER"
            echo "$CRON_LINE"
        ) | crontab -
        echo "✓ crontab 已装。验证："
        crontab -l | grep -A1 "飞书原生 RPA 定时发送"
        echo ""
        echo "下次跑: 整 6 点 (0:00, 6:00, 12:00, 18:00)。日志写到 $LOG"
        echo "看进度: tail -f $LOG"
        ;;
    remove)
        crontab -l 2>/dev/null | grep -vE "飞书原生 RPA 定时发送|feishu_native_rpa_sender\.py" | crontab -
        echo "✓ crontab 已卸"
        ;;
    status)
        echo "=== 当前 cron 条目 ==="
        if crontab -l 2>/dev/null | grep -q "飞书原生 RPA 定时发送"; then
            crontab -l | grep -A1 "飞书原生 RPA 定时发送"
            echo ""
            echo "=== 最近日志(末 30 行) ==="
            tail -30 "$LOG" 2>/dev/null || echo "(还没产生日志)"
        else
            echo "(未装)"
        fi
        ;;
    *)
        echo "用法:$0 {install|remove|status}"
        exit 1
        ;;
esac
