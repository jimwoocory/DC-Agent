#!/bin/bash
# 装/卸载「问卷填表 → 入职卡推送」轮询 cron
# 用法：
#   install-onboarding-watch-cron.sh install   装 cron（每 30 分钟跑一次）
#   install-onboarding-watch-cron.sh remove    卸
#   install-onboarding-watch-cron.sh status    看当前状态 + 最近日志

set -euo pipefail

DC_ROOT="/Users/dianchi/DC-Agent"
SCRIPT="$DC_ROOT/scripts-tools/check_and_push_onboarding.py"
PYTHON="$DC_ROOT/.venv/bin/python"
ENV_FILE="$HOME/.dc-agent.env"
LOG="$DC_ROOT/data/onboarding_watch.log"
MARKER="# DC-Agent 问卷→入职卡 轮询 (managed by scripts-tools/install-onboarding-watch-cron.sh)"

# 每 30 分钟跑一次。频率可以加快/放慢,按需改这一行。
CRON_LINE="*/30 * * * * cd '$DC_ROOT' && set -a && . '$ENV_FILE' && set +a && DC_AGENT_ROOT='$DC_ROOT' '$PYTHON' '$SCRIPT' >> '$LOG' 2>&1"

cmd="${1:-status}"

case "$cmd" in
    install)
        # 删旧 marker block,加新 block
        (
            crontab -l 2>/dev/null | grep -vE "问卷→入职卡 轮询|check_and_push_onboarding\.py" || true
            echo "$MARKER"
            echo "$CRON_LINE"
        ) | crontab -
        echo "✓ crontab 已装。验证："
        crontab -l | grep -A1 "问卷→入职卡 轮询"
        echo ""
        echo "下次跑:整点 / 半点(取决于当前几点)。日志写到 $LOG"
        echo "看进度: tail -f $LOG"
        ;;
    remove)
        crontab -l 2>/dev/null | grep -vE "问卷→入职卡 轮询|check_and_push_onboarding\.py" | crontab -
        echo "✓ crontab 已卸"
        # marker 文件保留,避免重装后重复推。要清空 marker:rm -rf $DC_ROOT/data/.onboarding_pushed_markers
        ;;
    status)
        echo "=== 当前 cron 条目 ==="
        if crontab -l 2>/dev/null | grep -q "问卷→入职卡 轮询"; then
            crontab -l | grep -A1 "问卷→入职卡 轮询"
            echo ""
            echo "=== marker 文件(已推过的 user_id) ==="
            ls -la "$DC_ROOT/data/.onboarding_pushed_markers" 2>/dev/null || echo "(还没推过)"
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
