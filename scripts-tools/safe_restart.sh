#!/bin/bash
# 安全主动重启 · 写 maintenance lock 让看门狗静默，重启完成自动删 lock
#
# 用法：
#   safe_restart.sh astrbot          # 重启 AstrBot（主聊天）
#   safe_restart.sh hermes-gateway   # 重启 Hermes Gateway
#   safe_restart.sh hermes-webui     # 重启 Hermes 官方 WebUI
#   safe_restart.sh hermes-webui-thirdparty  # 重启 Hermes 第三方 WebUI
#
# 比直接 launchctl kickstart 多做的：
#   1. 重启前写 data/watchdog/maintenance.lock
#   2. 等服务真就绪（端口 / HTTP 200）
#   3. 完成后删 lock
#   看门狗看到 lock 存在期间不触发告警（避免重启 30 秒被当事故）

set -euo pipefail

DC_ROOT="/Users/dianchi/DC-Agent"
WD_ROOT="$DC_ROOT/data/watchdog"
PRIMARY_RUNTIME="${DC_AGENT_PRIMARY_RUNTIME:-nas}"
LOCK_FILE="$WD_ROOT/maintenance.lock"
WATCHDOG_QUIET_SECONDS="${SAFE_RESTART_WATCHDOG_QUIET_SECONDS:-120}"
ASTRBOT_TMUX_SESSION="${SAFE_RESTART_ASTRBOT_TMUX_SESSION:-dc-agent-astrbot}"
SERVICE="${1:-}"

usage() {
    echo "用法: $(basename "$0") <service>"
    echo "  service: astrbot | hermes-gateway | hermes | hermes-webui | hermes-webui-thirdparty"
    exit 1
}

[ $# -lt 1 ] && usage

case "$SERVICE" in
    astrbot)
        if [ "$PRIMARY_RUNTIME" = "nas" ]; then
            echo "Local AstrBot restart skipped: NAS is the primary assistant runtime."
            exit 0
        fi
        LABEL="io.astrbot.bot"
        HEALTH_CMD="curl --noproxy '*' -sf --max-time 2 http://127.0.0.1:6185/api/stat/start-time"
        POST_HEALTH_CMD="$DC_ROOT/scripts-tools/card-system-health.py"
        DESC="AstrBot 主聊天"
        ;;
    hermes-gateway|hermes)
        LABEL="ai.hermes.gateway"
        HEALTH_CMD="lsof -nP -iTCP:8644 -sTCP:LISTEN"
        POST_HEALTH_CMD=""
        DESC="Hermes Gateway"
        ;;
    hermes-webui)
        LABEL="ai.hermes.dashboard"
        HEALTH_CMD="lsof -nP -iTCP:9119 -sTCP:LISTEN"
        POST_HEALTH_CMD=""
        DESC="Hermes 官方 WebUI"
        ;;
    hermes-webui-thirdparty|hermes-thirdparty-webui)
        LABEL="ai.hermes.webui.thirdparty"
        HEALTH_CMD="lsof -nP -iTCP:8787 -sTCP:LISTEN"
        POST_HEALTH_CMD=""
        DESC="Hermes 第三方 WebUI"
        ;;
    *)
        echo "❌ 未知服务: $1"
        usage
        ;;
esac

restart_astrbot_without_launchd() {
    echo "⚠️  launchd 未接管 AstrBot，改用当前部署方式重启：tmux/start-all.sh"

    if command -v tmux >/dev/null 2>&1; then
        tmux kill-session -t "$ASTRBOT_TMUX_SESSION" >/dev/null 2>&1 || true
    fi

    local pids
    pids="$(pgrep -f "([.]venv/bin/python|$DC_ROOT/.venv/bin/python) main.py" 2>/dev/null || true)"
    if [ -n "$pids" ]; then
        echo "🛑 停止现有 AstrBot 进程：$pids"
        for pid in $pids; do
            kill "$pid" >/dev/null 2>&1 || true
        done
        sleep 2
    fi

    if command -v tmux >/dev/null 2>&1; then
        tmux new-session -d -s "$ASTRBOT_TMUX_SESSION" \
            "cd $DC_ROOT && ./start-all.sh"
        echo "✅ AstrBot 已通过 tmux session=$ASTRBOT_TMUX_SESSION 启动"
        return 0
    fi

    "$DC_ROOT/start-all.sh" > "$WD_ROOT/astrbot_manual_restart.out" 2>&1 &
    echo "✅ AstrBot 已通过后台进程启动 pid=$!"
}

cleanup() {
    rm -f "$LOCK_FILE"
}
trap cleanup EXIT INT TERM

# 1. 写 maintenance lock
mkdir -p "$WD_ROOT"
cat > "$LOCK_FILE" <<EOF
{
  "service": "$LABEL",
  "desc": "$DESC",
  "started_at_unix": $(date +%s),
  "started_at_iso": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "started_by": "${USER:-unknown}",
  "pid": $$
}
EOF
echo "🔒 维护窗口开启（lock=${LOCK_FILE}，看门狗会跳过告警）"

# 2. kickstart 服务
echo "♻️  重启 $DESC ($LABEL) ..."
if ! launchctl kickstart -k "gui/$(id -u)/$LABEL"; then
    if [ "$SERVICE" = "astrbot" ]; then
        restart_astrbot_without_launchd
    else
        echo "❌ kickstart 下发失败 —— 可能 launchd 不认识这个 label"
        exit 1
    fi
fi

# 3. 等服务就绪
echo "⏳ 等待服务就绪（最多 120 秒）..."
TIMEOUT=120
START=$(date +%s)
while ! eval "$HEALTH_CMD" >/dev/null 2>&1; do
    sleep 2
    NOW=$(date +%s)
    ELAPSED=$((NOW - START))
    if [ "$ELAPSED" -ge "$TIMEOUT" ]; then
        echo "❌ 等待 ${TIMEOUT}s 超时，服务可能没起来 —— 看门狗 lock 即将释放，请立即排查"
        exit 1
    fi
done
ELAPSED=$(($(date +%s) - START))
echo "✅ $DESC 已就绪（耗时 ${ELAPSED}s）"

if [ -n "${POST_HEALTH_CMD:-}" ]; then
    echo "🧪 运行重启后工程自检：$POST_HEALTH_CMD"
    if ! "$POST_HEALTH_CMD"; then
        echo "❌ 重启后工程自检失败，请先修复再继续灰度"
        exit 1
    fi
    echo "🔗 自检后衔接："
    "$POST_HEALTH_CMD" --next-step || true
fi

if [ "$WATCHDOG_QUIET_SECONDS" -gt 0 ]; then
    echo "🕊️  继续保持 watchdog 静默 ${WATCHDOG_QUIET_SECONDS}s，等待联动探针稳定..."
    sleep "$WATCHDOG_QUIET_SECONDS"
fi

# 4. cleanup trap 会删 lock
echo "🔓 维护窗口关闭"
