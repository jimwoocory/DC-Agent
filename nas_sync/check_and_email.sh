#!/bin/bash
# ============================================================
# NAS 同步状态检查 + 邮件汇报
# 检查内容：
#   - 三个文件夹的本地大小、文件数
#   - 最近一次同步日志的成功/失败状态
#   - BaiduPCS-Go 是否还在运行
# ============================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NAS_DIR="/Users/dianchi/nas_kb/柳汽"
LOG_DIR="$SCRIPT_DIR/logs"

# ── 收集状态 ──────────────────────────────────────────────────
TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')
HOSTNAME=$(hostname)

# NAS 是否挂载
if mount | grep -q "/Users/dianchi/nas_kb"; then
    NAS_STATUS="✅ 已挂载"
else
    NAS_STATUS="❌ 未挂载"
fi

# 三个文件夹的统计
FOLDER_REPORT=""
for folder in "封面模板&尾版" "柳汽Q1季度素材成片汇总" "柳汽后市场新媒体运营视频"; do
    path="$NAS_DIR/$folder"
    if [ -d "$path" ]; then
        size=$(du -sh "$path" 2>/dev/null | awk '{print $1}')
        count=$(find "$path" -type f 2>/dev/null | wc -l | tr -d ' ')
        FOLDER_REPORT+="  • $folder：$size / $count 个文件\n"
    else
        FOLDER_REPORT+="  • $folder：⚠️ 目录不存在\n"
    fi
done

# 最近一次 sync 日志
LATEST_LOG=$(ls -t "$LOG_DIR"/sync_*.log 2>/dev/null | head -1)
SYNC_SUMMARY="(无日志)"
if [ -n "$LATEST_LOG" ]; then
    SYNC_SUMMARY=$(grep -E "结束 \||成功|失败|错误|✓|✗" "$LATEST_LOG" 2>/dev/null | tail -10)
fi

# BaiduPCS-Go 是否还在跑
RUNNING=$(pgrep -f "BaiduPCS-Go download" | wc -l | tr -d ' ')

# 最近 baidupcs 下载日志状态
BPS_LOG=$(ls -t "$LOG_DIR"/baidupcs_*.log 2>/dev/null | head -1)
BPS_SUMMARY="(无日志)"
FAILED_FILES=""
if [ -n "$BPS_LOG" ]; then
    # 统计成功/失败数
    # grep -c 在某些环境会输出多行，用 tr 净化为单一整数
    SUCCESS_COUNT=$(grep -c "下载完成, 保存位置" "$BPS_LOG" 2>/dev/null | head -1 | tr -d '\n ' || echo 0)
    FAIL_COUNT=$(grep -c "下载文件失败" "$BPS_LOG" 2>/dev/null | head -1 | tr -d '\n ' || echo 0)
    [ -z "$SUCCESS_COUNT" ] && SUCCESS_COUNT=0
    [ -z "$FAIL_COUNT" ] && FAIL_COUNT=0
    DOWNLOAD_END=$(grep "下载结束, 时间" "$BPS_LOG" 2>/dev/null | tail -1)
    BPS_SUMMARY="本次任务：成功 ${SUCCESS_COUNT} / 失败 ${FAIL_COUNT}
${DOWNLOAD_END:-(还在下载中)}
日志文件: $(basename "$BPS_LOG")"

    # 如果有失败，列出失败文件清单
    if [ "$FAIL_COUNT" -gt 0 ]; then
        FAILED_FILES=$(awk '/^以下文件下载失败:/{flag=1; next} flag' "$BPS_LOG" 2>/dev/null | head -30)
    fi
fi

# ── 拼成邮件正文 ──────────────────────────────────────────────
BODY=$(cat <<EOF
NAS 同步状态报告
=====================================
检查时间: $TIMESTAMP
主机: $HOSTNAME
NAS 状态: $NAS_STATUS

📁 文件夹统计 (/Users/dianchi/nas_kb/柳汽/)
$(echo -e "$FOLDER_REPORT")

🔄 BaiduPCS-Go 进程: $RUNNING 个在运行

📋 最近同步日志摘要:
$SYNC_SUMMARY

📥 最近下载摘要:
$BPS_SUMMARY

$([ -n "$FAILED_FILES" ] && echo "❌ 下载失败的文件:
$FAILED_FILES")

=====================================
日志路径: $LOG_DIR
EOF
)

# ── 发送邮件 ──────────────────────────────────────────────────
TO_EMAIL="jimwoo.cory@gmail.com"
FROM_EMAIL="${SMTP_FROM:-}"
SMTP_PASS="${SMTP_PASS:-}"
SMTP_HOST="${SMTP_HOST:-smtp.gmail.com}"
SMTP_PORT="${SMTP_PORT:-587}"
SUBJECT="[NAS同步报告] $(date '+%Y-%m-%d %H:%M')"

if [ -z "$FROM_EMAIL" ] || [ -z "$SMTP_PASS" ]; then
    echo "ERROR: 请设置环境变量 SMTP_FROM 和 SMTP_PASS"
    echo "本应发送的内容："
    echo "----"
    echo "$BODY"
    exit 1
fi

# 用 Python 通过 SMTP 发送
python3 - <<PYEOF
import smtplib, ssl
from email.mime.text import MIMEText
from email.utils import formatdate
from email.header import Header

msg = MIMEText("""$BODY""", 'plain', 'utf-8')
msg['From'] = '$FROM_EMAIL'
msg['To'] = '$TO_EMAIL'
msg['Subject'] = Header('$SUBJECT', 'utf-8')
msg['Date'] = formatdate(localtime=True)

context = ssl.create_default_context()
try:
    with smtplib.SMTP('$SMTP_HOST', $SMTP_PORT, timeout=30) as server:
        server.starttls(context=context)
        server.login('$FROM_EMAIL', '$SMTP_PASS')
        server.send_message(msg)
    print("✓ 邮件已发送至 $TO_EMAIL")
except Exception as e:
    print(f"✗ 邮件发送失败: {e}")
    exit(1)
PYEOF
