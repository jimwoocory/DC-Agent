#!/bin/bash
# 装/卸载『巅池-技术』日报 cron
# 用法：
#   install-dianchi-tech-cron.sh install   装两条 cron（01:00 night + 09:30 report）
#   install-dianchi-tech-cron.sh remove    卸
#   install-dianchi-tech-cron.sh status    看当前状态 + 最近日志

set -euo pipefail

DC_ROOT="/Users/dianchi/DC-Agent"
NIGHT="$DC_ROOT/scripts-tools/dianchi-tech-night.sh"
REPORT="$DC_ROOT/scripts-tools/dianchi-tech-report.sh"
LOG="$DC_ROOT/data/dianchi_tech/cron.log"
MARKER="# DC-Agent 巅池-技术 日报 (managed by scripts-tools/install-dianchi-tech-cron.sh)"

# 01:00 北京 = 美西 10:00 PT（夏令时 PDT，差 15h）
# 09:00 北京 = 蔡挺上班前到岗就能看到
NIGHT_LINE="0 1 * * * '$NIGHT' >> '$LOG' 2>&1"
REPORT_LINE="0 9 * * * '$REPORT' >> '$LOG' 2>&1"

cmd="${1:-status}"

case "$cmd" in
    install)
        # 删旧 marker block，加新 block
        (
            crontab -l 2>/dev/null | grep -vE "巅池-技术 日报|dianchi-tech-(night|report)\.sh" || true
            echo "$MARKER"
            echo "$NIGHT_LINE"
            echo "$REPORT_LINE"
        ) | crontab -
        echo "✓ crontab 已装。验证："
        crontab -l | grep -A2 "巅池-技术 日报"
        echo ""
        echo "下次跑：今晚 01:00（night）/ 明早 09:30（report）"
        ;;
    remove)
        crontab -l 2>/dev/null | grep -vE "巅池-技术 日报|dianchi-tech-(night|report)\.sh" | crontab -
        echo "✓ crontab 已卸"
        ;;
    status)
        echo "=== 当前 cron 条目 ==="
        if crontab -l 2>/dev/null | grep -q "巅池-技术 日报"; then
            crontab -l | grep -A2 "巅池-技术 日报"
            echo ""
            echo "=== 最近日志（cron.log 末 30 行）==="
            tail -30 "$LOG" 2>/dev/null || echo "(还没产生日志)"
        else
            echo "(未装)"
        fi
        ;;
    *)
        echo "用法：$0 {install|remove|status}"
        exit 1
        ;;
esac
