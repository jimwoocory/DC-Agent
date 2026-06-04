#!/bin/bash
# 安全主动重启 · 写 maintenance lock 让看门狗静默，重启完成自动删 lock
#
# 用法：
#   safe_restart.sh astrbot          # 重启 AstrBot（主聊天）
#   safe_restart.sh hermes-gateway   # 重启 Hermes Gateway
#   safe_restart.sh hermes-webui     # 重启 Hermes 官方 WebUI
#   safe_restart.sh hermes-webui-thirdparty  # 重启 Hermes 第三方 WebUI
#   safe_restart.sh openclaw         # 重启 OpenClaw watchdog
#
# 比直接 launchctl kickstart 多做的：
#   1. 重启前写 data/watchdog/maintenance.lock
#   2. 等服务真就绪（端口 / HTTP 200）
#   3. 完成后删 lock
#   看门狗看到 lock 存在期间不触发告警（避免重启 30 秒被当事故）

set -euo pipefail

DC_ROOT="/Users/dianchi/DC-Agent"
WD_ROOT="$DC_ROOT/data/watchdog"
LOCK_FILE="$WD_ROOT/maintenance.lock"

usage() {
    echo "用法: $(basename "$0") <service>"
    echo "  service: astrbot | hermes-gateway | hermes | hermes-webui | hermes-webui-thirdparty | openclaw"
    exit 1
}

[ $# -lt 1 ] && usage

case "$1" in
    astrbot)
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
    openclaw)
        LABEL="com.dcagent.openclaw-watchdog"
        HEALTH_CMD="lsof -nP -iTCP:9120 -sTCP:LISTEN"
        POST_HEALTH_CMD=""
        DESC="OpenClaw Watchdog"
        ;;
    *)
        echo "❌ 未知服务: $1"
        usage
        ;;
esac

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
    echo "❌ kickstart 下发失败 —— 可能 launchd 不认识这个 label"
    exit 1
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

# 4. cleanup trap 会删 lock
echo "🔓 维护窗口关闭"
