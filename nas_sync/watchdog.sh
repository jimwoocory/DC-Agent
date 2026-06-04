#!/bin/bash
# ============================================================
# NAS 挂载看门狗 v2
# 每 60 秒由 launchd 调用一次
# 智能流程：
#   1. NAS 可达性 → 检查挂载 → 检查健康
#   2. 挂载失败时识别根因（凭据/网络/SMB）
#   3. 凭据缺失时从备份自愈，仍失败则报警并退避
# ============================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MOUNT_SH="$SCRIPT_DIR/mount.sh"
MOUNT_POINT="/Users/dianchi/nas_kb"
NAS_IP="192.168.1.35"
LOG="$SCRIPT_DIR/watchdog.log"
HEARTBEAT="$SCRIPT_DIR/watchdog.heartbeat"
FAIL_STATE="$SCRIPT_DIR/watchdog.failstate"      # 存连续失败次数 + 最近告警时间
ALERT_SCRIPT="$SCRIPT_DIR/check_and_email.sh"     # 失败时复用邮件脚本告警

# 凭据相关
KEYCHAIN_KEY="dc-agent-nas-password"
KEYCHAIN_ACCOUNT="dcwh-001"
PASSWORD_BACKUP="$HOME/.config/nas_sync/nas_password.bak"  # 兜底密码文件 (chmod 600)

# 告警阈值：连续 N 次失败后报警，之后 ALERT_COOLDOWN 秒内不再重复发邮件
ALERT_THRESHOLD=3
ALERT_COOLDOWN=3600

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG"; }
touch_heartbeat() { touch "$HEARTBEAT" 2>/dev/null || true; }

# 失败计数 / 告警节流
get_fail_count() { [ -f "$FAIL_STATE" ] && cut -d: -f1 "$FAIL_STATE" 2>/dev/null || echo 0; }
get_last_alert() { [ -f "$FAIL_STATE" ] && cut -d: -f2 "$FAIL_STATE" 2>/dev/null || echo 0; }
set_fail_state() {
    local count="$1" last_alert="$2"
    echo "${count}:${last_alert}" > "$FAIL_STATE"
}
reset_fail_state() { rm -f "$FAIL_STATE"; }

# 日志滚动
[ "$(wc -l < "$LOG" 2>/dev/null || echo 0)" -gt 1000 ] && \
    tail -500 "$LOG" > "${LOG}.tmp" && mv "${LOG}.tmp" "$LOG"

# ── 凭据健康检查 + 自愈 ────────────────────────────────────────
ensure_credentials() {
    # 检查 Keychain 是否有密码
    if security find-generic-password -s "$KEYCHAIN_KEY" -w &>/dev/null; then
        return 0  # 凭据 OK
    fi

    log "WARN  Keychain 中缺失 $KEYCHAIN_KEY"

    # 尝试从备份恢复
    if [ -r "$PASSWORD_BACKUP" ]; then
        local pwd
        pwd=$(cat "$PASSWORD_BACKUP")
        if [ -n "$pwd" ] && security add-generic-password \
                -s "$KEYCHAIN_KEY" -a "$KEYCHAIN_ACCOUNT" \
                -w "$pwd" -U 2>>"$LOG"; then
            log "HEAL  已从 $PASSWORD_BACKUP 自动恢复 Keychain 密码"
            return 0
        fi
    fi

    log "FAIL  无法恢复凭据 (备份文件: $PASSWORD_BACKUP 不存在或读取失败)"
    return 1
}

# ── 失败处理：计数 + 告警节流 ──────────────────────────────────
record_failure() {
    local reason="$1"
    local count
    count=$(($(get_fail_count) + 1))
    local last_alert
    last_alert=$(get_last_alert)
    local now
    now=$(date +%s)

    log "FAIL  $reason (连续失败 $count 次)"

    # 达到阈值且距上次告警超过冷却时间 → 发邮件
    if [ "$count" -ge "$ALERT_THRESHOLD" ] && \
       [ "$((now - last_alert))" -ge "$ALERT_COOLDOWN" ]; then
        log "ALERT 触发告警邮件 (失败 $count 次)"
        # 后台发邮件，不阻塞看门狗
        if [ -f "$HOME/.config/nas_sync/smtp.env" ] && [ -x "$ALERT_SCRIPT" ]; then
            (
                set -a; source "$HOME/.config/nas_sync/smtp.env"; set +a
                bash "$ALERT_SCRIPT"
            ) >>"$LOG" 2>&1 &
        fi
        # 系统通知（Mac 屏幕右上角弹窗）
        osascript -e "display notification \"$reason\" with title \"NAS 看门狗告警\" sound name \"Basso\"" 2>/dev/null || true
        set_fail_state "$count" "$now"
    else
        set_fail_state "$count" "$last_alert"
    fi
}

# ── 1. NAS 可达性 ─────────────────────────────────────────────
# 直接探测 SMB 端口，比 ICMP 更贴近真实挂载条件；也避免 macOS ping
# 在不可达时被 -W 参数拖很久，导致 launchd 周期里堆积进程。
if ! nc -z -G 2 "$NAS_IP" 445 &>/dev/null; then
    touch_heartbeat
    log "SKIP  NAS $NAS_IP:445 不可达"
    exit 0
fi

# ── 2. 检查挂载状态 ────────────────────────────────────────────
is_mounted() {
    mount | grep -q "on ${MOUNT_POINT} "
}

is_healthy() {
    /usr/bin/python3 - "$NAS_IP" <<'PY'
import subprocess, sys
nas_ip = sys.argv[1]
try:
    result = subprocess.run(
        ["/usr/bin/smbutil", "statshares", "-a"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        timeout=3, check=True, text=True,
    )
except Exception:
    sys.exit(1)
sys.exit(0 if f"SERVER_NAME                   {nas_ip}" in result.stdout else 1)
PY
}

if is_mounted; then
    if is_healthy; then
        touch_heartbeat
        reset_fail_state    # 一切正常，清零失败计数
        exit 0              # 安静退出
    else
        log "STALE 挂载点僵死，强制卸载..."
        diskutil unmount force "$MOUNT_POINT" 2>>"$LOG" || \
            umount -f "$MOUNT_POINT" 2>>"$LOG"
    fi
fi

# ── 3. 挂载前先确保凭据可用 ───────────────────────────────────
if ! ensure_credentials; then
    touch_heartbeat
    record_failure "凭据缺失：Keychain 无密码且备份文件不可用"
    exit 0
fi

# ── 4. 执行挂载 ───────────────────────────────────────────────
log "MOUNT 尝试挂载 $MOUNT_POINT ..."
MOUNT_OUTPUT=$(bash "$MOUNT_SH" mount 2>&1)
echo "$MOUNT_OUTPUT" >> "$LOG"
touch_heartbeat

if echo "$MOUNT_OUTPUT" | grep -q "挂载成功"; then
    log "OK    挂载成功"
    reset_fail_state
    exit 0
fi

# ── 5. 失败根因分析 ───────────────────────────────────────────
REASON="未知挂载错误"
if echo "$MOUNT_OUTPUT" | grep -qi "Authentication error\|not found in the keychain"; then
    REASON="认证失败：密码错误或 Keychain 项损坏"
    # 删掉错误的 keychain 项，下次会从备份重建
    security delete-generic-password -s "$KEYCHAIN_KEY" &>/dev/null
    log "HEAL  已清除疑似损坏的 Keychain 项，下次将从备份重建"
elif echo "$MOUNT_OUTPUT" | grep -qi "Connection refused\|Network is unreachable"; then
    REASON="网络问题：SMB 端口不可达"
elif echo "$MOUNT_OUTPUT" | grep -qi "Permission denied"; then
    REASON="权限错误：账号无权访问该共享"
fi

record_failure "$REASON"
