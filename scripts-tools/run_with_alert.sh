#!/bin/bash
# ============================================================
# 通用定时任务告警包装器
# 用法：
#   bash run_with_alert.sh <task-name> <executable> [args...]
#
# 例如在 plist 里把
#   /bin/bash /path/to/sync.sh
# 改成：
#   /bin/bash /Users/dianchi/DC-Agent/scripts-tools/run_with_alert.sh \
#       baidu-nas-sync /path/to/sync.sh
#
# 行为：
#   - 执行目标命令，记录开始/结束时间、退出码、运行时长
#   - 退出码 != 0 → 增加失败计数，达到阈值发邮件告警
#   - 退出码 == 0 → 清零失败计数，更新心跳
#   - 超时熔断：任务跑超过 TIMEOUT_SECS 会被 kill 并告警
#
# 状态文件位置（每任务独立）：
#   ~/.config/nas_sync/alert_state/<task-name>.state   连续失败次数:上次告警时间戳
#   ~/.config/nas_sync/alert_state/<task-name>.heartbeat   最后成功运行时间
#   ~/.config/nas_sync/alert_state/<task-name>.log     运行历史
# ============================================================

set -u

# ── 参数 ──────────────────────────────────────────────────────
if [ $# -lt 2 ]; then
    echo "用法: $0 <task-name> <executable> [args...]" >&2
    exit 64
fi

TASK_NAME="$1"
shift
EXEC="$1"
shift
ARGS=("$@")

# ── 配置 ──────────────────────────────────────────────────────
STATE_DIR="$HOME/.config/nas_sync/alert_state"
SMTP_ENV="$HOME/.config/nas_sync/smtp.env"
ALERT_THRESHOLD="${ALERT_THRESHOLD:-2}"      # 连续 N 次失败后告警
ALERT_COOLDOWN="${ALERT_COOLDOWN:-3600}"     # 告警冷却 1 小时
TIMEOUT_SECS="${TIMEOUT_SECS:-7200}"         # 任务最长执行 2 小时
TO_EMAIL="${TO_EMAIL:-jimwoo.cory@gmail.com}"

mkdir -p "$STATE_DIR"
STATE_FILE="$STATE_DIR/$TASK_NAME.state"
HEARTBEAT="$STATE_DIR/$TASK_NAME.heartbeat"
HISTORY="$STATE_DIR/$TASK_NAME.log"

# ── 工具函数 ──────────────────────────────────────────────────
ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "[$(ts)] $*" >> "$HISTORY"; }

get_fail_count() { [ -f "$STATE_FILE" ] && cut -d: -f1 "$STATE_FILE" 2>/dev/null || echo 0; }
get_last_alert() { [ -f "$STATE_FILE" ] && cut -d: -f2 "$STATE_FILE" 2>/dev/null || echo 0; }
set_state() { echo "${1}:${2}" > "$STATE_FILE"; }
reset_state() { rm -f "$STATE_FILE"; }

# 日志滚动
if [ -f "$HISTORY" ] && [ "$(wc -l < "$HISTORY" 2>/dev/null || echo 0)" -gt 500 ]; then
    tail -200 "$HISTORY" > "${HISTORY}.tmp" && mv "${HISTORY}.tmp" "$HISTORY"
fi

# ── 邮件告警 ──────────────────────────────────────────────────
send_alert() {
    local subject="$1"
    local body="$2"

    if [ ! -f "$SMTP_ENV" ]; then
        log "ALERT  $subject — 但 SMTP 配置缺失，仅本地通知"
        osascript -e "display notification \"$subject\" with title \"任务告警: $TASK_NAME\" sound name \"Basso\"" 2>/dev/null || true
        return 1
    fi

    set -a; source "$SMTP_ENV"; set +a

    /usr/bin/python3 - <<PYEOF >> "$HISTORY" 2>&1
import smtplib, ssl, os
from email.mime.text import MIMEText
from email.utils import formatdate
from email.header import Header

subject = """$subject"""
body = """$body"""
to_addr = "$TO_EMAIL"
from_addr = os.environ.get("SMTP_FROM", "")
smtp_pass = os.environ.get("SMTP_PASS", "")
smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
smtp_port = int(os.environ.get("SMTP_PORT", "587"))

msg = MIMEText(body, 'plain', 'utf-8')
msg['From'] = from_addr
msg['To'] = to_addr
msg['Subject'] = Header(subject, 'utf-8')
msg['Date'] = formatdate(localtime=True)

try:
    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
        server.starttls(context=ssl.create_default_context())
        server.login(from_addr, smtp_pass)
        server.send_message(msg)
    print(f"[$(ts)] ALERT_MAIL_OK  {subject}")
except Exception as e:
    print(f"[$(ts)] ALERT_MAIL_FAIL  {e}")
PYEOF
    osascript -e "display notification \"$subject\" with title \"任务告警: $TASK_NAME\" sound name \"Basso\"" 2>/dev/null || true
}

# ── 失败处理 ──────────────────────────────────────────────────
record_failure() {
    local reason="$1"
    local last_output="$2"
    local count
    count=$(($(get_fail_count) + 1))
    local last_alert
    last_alert=$(get_last_alert)
    local now
    now=$(date +%s)

    log "FAIL   $reason (连续失败 $count 次)"

    if [ "$count" -ge "$ALERT_THRESHOLD" ] && [ "$((now - last_alert))" -ge "$ALERT_COOLDOWN" ]; then
        local subject="[任务失败] $TASK_NAME 连续 $count 次"
        local body
        body=$(cat <<EOF
任务名称: $TASK_NAME
失败次数: $count（连续）
失败原因: $reason
检查时间: $(ts)
主机: $(hostname)

最近输出（最后 50 行）:
─────────────────────────────────────
$last_output
─────────────────────────────────────

历史日志: $HISTORY
心跳文件: $HEARTBEAT
状态文件: $STATE_FILE

告警阈值: 连续 $ALERT_THRESHOLD 次
告警冷却: $ALERT_COOLDOWN 秒
EOF
)
        send_alert "$subject" "$body"
        set_state "$count" "$now"
    else
        set_state "$count" "$last_alert"
    fi
}

# ── 主流程 ────────────────────────────────────────────────────
START_TS=$(date +%s)
log "START  $EXEC ${ARGS[*]:-} (timeout=${TIMEOUT_SECS}s)"

# 用 perl 实现超时（macOS 自带 timeout 不一定有；perl 一定有）
OUTPUT_FILE=$(mktemp -t "alert_${TASK_NAME}.XXXXXX")
trap 'rm -f "$OUTPUT_FILE"' EXIT

perl -e '
    use strict; use warnings;
    my ($timeout, @cmd) = @ARGV;
    my $pid = fork();
    if ($pid == 0) { exec @cmd; exit 127; }
    local $SIG{ALRM} = sub {
        kill "TERM", $pid;
        sleep 2;
        kill "KILL", $pid;
        exit 124;
    };
    alarm $timeout;
    waitpid($pid, 0);
    exit($? >> 8);
' "$TIMEOUT_SECS" "$EXEC" "${ARGS[@]}" > "$OUTPUT_FILE" 2>&1 &

CHILD_PID=$!
wait $CHILD_PID
RC=$?

END_TS=$(date +%s)
ELAPSED=$((END_TS - START_TS))

# 截取输出末尾给告警邮件用（最多 50 行）
LAST_OUTPUT=$(tail -50 "$OUTPUT_FILE" 2>/dev/null || echo "(无输出)")

# 把完整输出追加到 history 日志，加缩进便于阅读
{
    echo "─── output begin ───"
    cat "$OUTPUT_FILE"
    echo "─── output end ───"
} >> "$HISTORY"

# 判断成败
if [ "$RC" -eq 0 ]; then
    touch "$HEARTBEAT"
    reset_state
    log "OK     退出码 0，耗时 ${ELAPSED}s"
    exit 0
elif [ "$RC" -eq 124 ]; then
    log "TIMEOUT  超过 ${TIMEOUT_SECS}s 被熔断"
    record_failure "执行超时（>${TIMEOUT_SECS}s）" "$LAST_OUTPUT"
    exit 124
else
    log "FAIL   退出码 $RC，耗时 ${ELAPSED}s"
    record_failure "退出码 $RC" "$LAST_OUTPUT"
    exit "$RC"
fi
