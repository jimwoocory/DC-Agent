#!/bin/bash
# launchd 入口：加载 SMTP 凭据 → 跑检查脚本 → 自删（仅运行一次）
set -a
source /Users/dianchi/.config/nas_sync/smtp.env
set +a

bash /Users/dianchi/DC-Agent/nas_sync/check_and_email.sh

# 一次性任务：跑完自动卸载并删除 plist
PLIST="/Users/dianchi/Library/LaunchAgents/com.dcagent.nas-status-mail.plist"
launchctl unload "$PLIST" 2>/dev/null
rm -f "$PLIST"
echo "[$(date '+%F %T')] 任务已完成并自动卸载" >> /Users/dianchi/DC-Agent/nas_sync/logs/oneshot_mail.log
