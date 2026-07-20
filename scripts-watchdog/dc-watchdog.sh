#!/bin/bash
# DC-Agent 全栈健康探活（cron 每分钟跑一次）
#
# 职责：
#   1. 探关键 TCP 端口、HTTP business endpoint、文件心跳和 dashboard 快捷入口
#   2. 对每个 probe 比较 state.json 里上次状态，变化时 emit 事件
#   3. ok→fail 事件触发 codex 自动诊断（diagnose.sh）+ macOS 通知
#   4. fail→ok 事件写"已恢复"通知
#   5. 所有事件 append 进 alerts.jsonl
#
# 不做的事（避坑）：
#   - 不直接接管进程生命周期（让 launchd KeepAlive 自己处理）
#   - failure 持续期间不重复诊断（30 分钟冷却）

set -euo pipefail

# cron 跑时 PATH 空，显式设
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/Users/dianchi/.local/bin"

# Keep local health checks off any developer proxy inherited by cron/launchd.
LOCAL_NO_PROXY="127.0.0.1,localhost,::1"
export NO_PROXY="${NO_PROXY:+$NO_PROXY,}$LOCAL_NO_PROXY"
export no_proxy="${no_proxy:+$no_proxy,}$LOCAL_NO_PROXY"

DC_ROOT="/Users/dianchi/DC-Agent"
WD_ROOT="$DC_ROOT/data/watchdog"
PRIMARY_RUNTIME="${DC_AGENT_PRIMARY_RUNTIME:-nas}"
STATE_FILE="$WD_ROOT/state.json"
ALERTS_LOG="$WD_ROOT/alerts.jsonl"
INCIDENT_DIR="$WD_ROOT/incidents"
COOLDOWN_SEC=1800  # 30 分钟内同一 service 失败只诊断一次
AGENT_MAINTENANCE_GRACE_SEC=900  # Codex / Claude Code 维护窗口：15 分钟内不触发诊断推送
RESTART_GRACE_SEC=900  # 人为/launchd 重启抖动：15 分钟内不触发诊断推送

mkdir -p "$INCIDENT_DIR"
touch "$ALERTS_LOG"
[ -f "$STATE_FILE" ] || echo '{}' > "$STATE_FILE"

# ─────────────────── 维护窗口抑制（safe_restart.sh 配套）───────────────────
# 主动重启服务时 safe_restart.sh 会写这个 lock，期间看门狗静默
# lock > 10 分钟没删 → 视为脚本异常退出忘了删 → 主动清掉避免永久静默
MAINT_LOCK="$WD_ROOT/maintenance.lock"
if [ -f "$MAINT_LOCK" ]; then
    if [ -n "$(find "$MAINT_LOCK" -mmin +10 2>/dev/null)" ]; then
        echo "[watchdog] maintenance lock > 10min 自动清除（防永久静默）" >&2
        rm -f "$MAINT_LOCK"
    else
        # lock 还新，跳过本轮探测
        exit 0
    fi
fi

# Review reminders are independent from probe evaluation and run under their
# own non-blocking lock. Notification failure never delays the health loop.
nohup "$DC_ROOT/.venv/bin/python" \
    "$DC_ROOT/scripts-watchdog/review_notifier.py" \
    >/dev/null 2>>"$WD_ROOT/review_notifier.log" &
disown $! 2>/dev/null || true

# Refresh a derived, read-only repair reliability snapshot. The script gates
# itself to one refresh per five minutes and never deletes audit history.
nohup "$DC_ROOT/.venv/bin/python" \
    "$DC_ROOT/scripts-watchdog/repair_analytics.py" \
    >/dev/null 2>>"$WD_ROOT/repair_analytics.log" &
disown $! 2>/dev/null || true

now_iso() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
now_ts() { date +%s; }

# 探一个 TCP 端口（返 ok / fail）
probe_tcp() {
    local port="$1"
    if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
        echo "ok"
    else
        echo "fail"
    fi
}

# 探一个 HTTP endpoint
# 算 ok 的 code：
#   2xx/3xx → 正常响应
#   401     → 路由存在但需要 auth；对于探活而言 = plugin / handler 在跑
# 算 fail 的 code：
#   000     → 连不上
#   4xx 其他 / 5xx → 服务异常
probe_http() {
    local url="$1"
    local code
    if ! code=$(curl --noproxy "*" -s -o /dev/null -w "%{http_code}" --max-time 3 "$url" 2>/dev/null); then
        code="000"
    fi
    if [[ "$code" =~ ^[23] ]] || [ "$code" = "401" ]; then
        echo "ok"
    else
        echo "fail:$code"
    fi
}

# 探一个必须 2xx 的 HTTP endpoint；401/403 不能算健康。
probe_http_strict() {
    local url="$1"
    local code
    if ! code=$(curl --noproxy "*" -s -o /dev/null -w "%{http_code}" --max-time 3 "$url" 2>/dev/null); then
        code="000"
    fi
    if [[ "$code" =~ ^2 ]]; then
        echo "ok"
    else
        echo "fail:$code"
    fi
}

# 探一个文件 mtime 新鲜度（用 stat），target 格式 "<path>:<max_age_seconds>"
# 用例：检查 nas_sync/watchdog.log 最近 180s 内有更新，证明老 watchdog launchd 在跑
probe_file_age() {
    local target="$1"
    local path="${target%:*}"
    local max_age="${target##*:}"
    if [ ! -f "$path" ]; then
        echo "fail:missing"
        return
    fi
    local mtime now age
    mtime=$(stat -f "%m" "$path" 2>/dev/null || echo 0)
    now=$(date +%s)
    age=$((now - mtime))
    if [ "$age" -le "$max_age" ]; then
        echo "ok"
    else
        echo "fail:stale_${age}s"
    fi
}

# 检查 dashboard 顶栏快捷入口是否仍安装在当前 data/dist。
# AstrBot 升级可能覆盖 data/dist/index.html，这个探活能快速发现入口丢失。
probe_dashboard_quick_entries() {
    if python3 "$DC_ROOT/scripts-tools/install_dashboard_quick_entries.py" --check --quiet >/dev/null 2>&1; then
        echo "ok"
    else
        echo "fail:missing_or_stale"
    fi
}

# Kick the NAS/Feishu/KB knowledge scheduler without blocking this watchdog.
probe_knowledge_cycle() {
    local code
    if "$DC_ROOT/.venv/bin/python" "$DC_ROOT/scripts-watchdog/knowledge_cycle.py" --tick >/dev/null 2>&1; then
        echo "ok"
    else
        code=$?
        echo "fail:$code"
    fi
}

# JSON 状态读 / 写（小型 jq-free 操作）
state_get() {
    local key="$1"
    python3 -c "import json,sys; d=json.load(open('$STATE_FILE')); print(json.dumps(d.get('$key', {})))"
}
state_set() {
    local key="$1" status="$2" since="$3"
    python3 -c "
import json
p = '$STATE_FILE'
d = json.load(open(p))
d['$key'] = {'status': '$status', 'since': '$since'}
json.dump(d, open(p, 'w'), indent=2)
"
}

state_set_disabled() {
    local key="$1" reason="$2" since="$3"
    "$DC_ROOT/.venv/bin/python" "$DC_ROOT/scripts-watchdog/watchdog_state.py" \
        mark-disabled "$STATE_FILE" "$key" "$reason" "$since"
}

state_set_maintenance_deferred() {
    local key="$1" status="$2" since="$3" deferred_ts="$4" reason="$5"
    python3 - "$STATE_FILE" "$key" "$status" "$since" "$deferred_ts" "$reason" <<'PY'
import json
import sys

p, key, status, since, deferred_ts, reason = sys.argv[1:]
d = json.load(open(p))
d[key] = {
    'status': status,
    'since': since,
    'maintenance_deferred_ts': int(deferred_ts),
    'maintenance_reason': reason[:500],
}
json.dump(d, open(p, 'w'), indent=2)
PY
}

state_clear_maintenance_deferred() {
    local key="$1"
    python3 - "$STATE_FILE" "$key" <<'PY'
import json
import sys

p, key = sys.argv[1:]
d = json.load(open(p))
state = d.get(key)
if not isinstance(state, dict):
    sys.exit(0)
changed = False
for field in ("maintenance_deferred_ts", "maintenance_reason"):
    if field in state:
        state.pop(field, None)
        changed = True
if changed:
    json.dump(d, open(p, "w"), indent=2)
PY
}

state_get_maintenance_deferred_ts() {
    python3 -c "import json,sys; d=json.load(open('$STATE_FILE')); print(d.get('$1', {}).get('maintenance_deferred_ts', 0))"
}

state_get_maintenance_reason() {
    python3 -c "import json,sys; d=json.load(open('$STATE_FILE')); print(d.get('$1', {}).get('maintenance_reason', ''))"
}

trigger_diagnose() {
    local name="$1" kind="$2" target="$3" cur="$4"
    if in_cooldown "$name"; then
        emit_event "$name" "$kind:$target" "$cur" "$cur" "cooldown_skipped_diagnose"
    else
        incident_id="$(date +%s)-$name"
        write_incident "$name" "$kind:$target" "$cur" "$incident_id"
        mark_diag_done "$name"
        # 异步跑 diagnose（不阻塞下次 cron）
        nohup "$DC_ROOT/scripts-watchdog/diagnose.sh" "$incident_id" "$name" >/dev/null 2>&1 &
        disown $! 2>/dev/null || true
    fi
}

# 写一条 alert 进 jsonl
emit_event() {
    local service="$1" probe="$2" prev="$3" cur="$4" extra="$5"
    python3 -c "
import json, time
entry = {
    'ts': '$(now_iso)',
    'service': '$service',
    'probe': '$probe',
    'prev': '$prev',
    'cur': '$cur',
    'extra': '$extra',
}
open('$ALERTS_LOG','a').write(json.dumps(entry, ensure_ascii=False) + '\n')
"
}

# Codex / Claude Code 做日常维护时，经常会短暂重启 AstrBot / Hermes。
# 这类 ok→fail 是预期维护窗口，不应再触发 watchdog 的 Codex 诊断和飞书告警。
is_agent_maintenance_service() {
    local service="$1"
    "$DC_ROOT/.venv/bin/python" - "$service" "$DC_ROOT" <<'PY'
import importlib.util
import sys
from pathlib import Path

service = sys.argv[1]
root = Path(sys.argv[2])
module_path = root / "scripts-watchdog" / "watchdog_engine.py"
spec = importlib.util.spec_from_file_location("watchdog_engine", module_path)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
raise SystemExit(0 if module.should_suppress_for_agent_maintenance(service) else 1)
PY
}

agent_maintenance_reason() {
    "$DC_ROOT/.venv/bin/python" "$DC_ROOT/scripts-watchdog/watchdog_engine.py" \
        maintenance-reason \
        --grace-sec "$AGENT_MAINTENANCE_GRACE_SEC" \
        --dc-root "$DC_ROOT"
}

# 写一条 incident snapshot（codex 诊断的输入）
write_incident() {
    local service="$1" probe="$2" cur="$3" id="$4"
    python3 -c "
import json, subprocess
def safe_run(cmd):
    try:
        return subprocess.check_output(cmd, shell=True, stderr=subprocess.STDOUT, timeout=10).decode('utf-8', errors='ignore')
    except Exception as e:
        return f'(error: {e})'

snapshot = {
    'incident_id': '$id',
    'ts': '$(now_iso)',
    'service': '$service',
    'probe': '$probe',
    'cur_status': '$cur',
    'context': {
        'launchctl_list': safe_run('launchctl list | grep -E \"astrbot|hermes\" | head -30'),
        'pgrep_python': safe_run('ps -ef | grep -E \"python.*(main.py|hermes_cli|hermes-webui|server.py)\" | grep -v grep | head -20'),
        'lsof_ports': safe_run('lsof -nP -iTCP -sTCP:LISTEN 2>/dev/null | grep -E \":(6185|8644|8645|8787|9119)\" | head -30'),
        'astrbot_log_tail': safe_run('tail -40 /Users/dianchi/DC-Agent/astrbot.log 2>/dev/null'),
        'hermes_log_tail': safe_run('tail -30 /Users/dianchi/DC-Agent/hermes-config/logs/gateway.log 2>/dev/null'),
        'hermes_err_tail': safe_run('tail -10 /Users/dianchi/DC-Agent/hermes-config/logs/gateway.error.log 2>/dev/null'),
        'thirdparty_webui_out_tail': safe_run('tail -30 /Users/dianchi/DC-Agent/logs/hermes-webui-thirdparty.out.log 2>/dev/null'),
        'thirdparty_webui_err_tail': safe_run('tail -30 /Users/dianchi/DC-Agent/logs/hermes-webui-thirdparty.err.log 2>/dev/null'),
        'knowledge_cycle_tail': safe_run('tail -50 /Users/dianchi/DC-Agent/data/watchdog/knowledge_cycle.log 2>/dev/null'),
    },
}
open('$INCIDENT_DIR/incident-$id.json','w').write(json.dumps(snapshot, indent=2, ensure_ascii=False))
"
}

# 检查 cooldown：上次诊断在 COOLDOWN_SEC 秒之内就跳过
in_cooldown() {
    local service="$1"
    local last_diag
    last_diag=$(python3 -c "
import json
d = json.load(open('$STATE_FILE'))
print(d.get('$service', {}).get('last_diag_ts', 0))
")
    local now=$(now_ts)
    local diff=$((now - last_diag))
    [ "$diff" -lt "$COOLDOWN_SEC" ]
}

mark_diag_done() {
    local service="$1"
    local ts=$(now_ts)
    python3 -c "
import json
p = '$STATE_FILE'
d = json.load(open(p))
d.setdefault('$service', {})['last_diag_ts'] = $ts
json.dump(d, open(p, 'w'), indent=2)
"
}

# ────────────────── 主流程 ──────────────────

# service 清单由 watchdog_engine.py 统一维护，避免 dc-watchdog.sh 和
# watchdogctl.py 各自硬编码一套探针规则。
SERVICES=()
while IFS= read -r entry; do
    [ -n "$entry" ] && SERVICES+=("$entry")
done < <("$DC_ROOT/.venv/bin/python" "$DC_ROOT/scripts-watchdog/watchdog_engine.py" list-active --runtime "$PRIMARY_RUNTIME")

DISABLED_SERVICES=()
while IFS= read -r entry; do
    [ -n "$entry" ] && DISABLED_SERVICES+=("$entry")
done < <("$DC_ROOT/.venv/bin/python" "$DC_ROOT/scripts-watchdog/watchdog_engine.py" list-disabled --runtime "$PRIMARY_RUNTIME")

for entry in "${DISABLED_SERVICES[@]}"; do
    IFS='|' read -r name reason <<< "$entry"
    state_set_disabled "$name" "$reason" "$(now_iso)"
done

for entry in "${SERVICES[@]}"; do
    IFS='|' read -r name kind target <<< "$entry"
    case "$kind" in
        tcp) cur=$(probe_tcp "$target") ;;
        http) cur=$(probe_http "$target") ;;
        http_strict) cur=$(probe_http_strict "$target") ;;
        file_age) cur=$(probe_file_age "$target") ;;
        dashboard_quick_entries) cur=$(probe_dashboard_quick_entries) ;;
        knowledge_cycle) cur=$(probe_knowledge_cycle) ;;
        *) cur="fail:unknown_probe_type" ;;
    esac

    prev_state=$(state_get "$name")
    prev_status=$(echo "$prev_state" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('status','unknown'))")

    # 跟上次状态比，只在 transition 时 emit
    cur_simple=$(echo "$cur" | cut -d: -f1)
    prev_simple=$(echo "$prev_status" | cut -d: -f1)

    if [ "$cur_simple" = "ok" ]; then
        state_clear_maintenance_deferred "$name"
    fi

    if [ "$cur_simple" = "fail" ] && [ "$prev_simple" = "fail" ] && is_agent_maintenance_service "$name"; then
        deferred_ts=$(state_get_maintenance_deferred_ts "$name")
        maintenance_reason=$(state_get_maintenance_reason "$name")
        if [ "$deferred_ts" != "0" ] && [[ "$maintenance_reason" == restart_grace:* ]]; then
            now_sec=$(now_ts)
            deferred_age=$((now_sec - deferred_ts))
            if [ "$deferred_age" -lt "$RESTART_GRACE_SEC" ]; then
                emit_event "$name" "$kind:$target" "$prev_status" "$cur" "restart_grace_skipped_diagnose:${deferred_age}s"
                state_set_maintenance_deferred "$name" "$cur" "$(now_iso)" "$deferred_ts" "$maintenance_reason"
                continue
            fi
            emit_event "$name" "$kind:$target" "$prev_status" "$cur" "restart_grace_expired:${deferred_age}s"
            state_clear_maintenance_deferred "$name"
            trigger_diagnose "$name" "$kind" "$target" "$cur"
        fi
    fi

    if [ "$cur_simple" != "$prev_simple" ]; then
        emit_event "$name" "$kind:$target" "$prev_status" "$cur" ""
        state_set "$name" "$cur" "$(now_iso)"

        if [ "$cur_simple" = "fail" ]; then
            # Normal restarts briefly drop multiple ports/endpoints. Do not alert
            # immediately for restart-prone services; diagnose only if the
            # failure survives the grace window above.
            if is_agent_maintenance_service "$name"; then
                now_sec=$(now_ts)
                reason="restart_grace:${RESTART_GRACE_SEC}s"
                emit_event "$name" "$kind:$target" "$prev_status" "$cur" "restart_grace_deferred_diagnose:$reason"
                state_set_maintenance_deferred "$name" "$cur" "$(now_iso)" "$now_sec" "$reason"
                continue
            fi
            # ok → fail：触发诊断（带 cooldown）
            if is_agent_maintenance_service "$name" && reason=$(agent_maintenance_reason); then
                deferred_ts=$(echo "$prev_state" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('maintenance_deferred_ts', 0))")
                now_sec=$(now_ts)
                if [ "$deferred_ts" = "0" ]; then
                    emit_event "$name" "$kind:$target" "$prev_status" "$cur" "agent_maintenance_deferred_diagnose:$reason"
                    state_set_maintenance_deferred "$name" "$prev_status" "$(now_iso)" "$now_sec" "$reason"
                    continue
                fi
                deferred_age=$((now_sec - deferred_ts))
                if [ "$deferred_age" -lt "$AGENT_MAINTENANCE_GRACE_SEC" ]; then
                    emit_event "$name" "$kind:$target" "$prev_status" "$cur" "agent_maintenance_skipped_diagnose:${deferred_age}s:$reason"
                    state_set_maintenance_deferred "$name" "$prev_status" "$(now_iso)" "$deferred_ts" "$reason"
                    continue
                fi
                emit_event "$name" "$kind:$target" "$prev_status" "$cur" "agent_maintenance_grace_expired:${deferred_age}s"
                if in_cooldown "$name"; then
                    emit_event "$name" "$kind:$target" "$prev_status" "$cur" "cooldown_skipped_diagnose"
                else
                    incident_id="$(date +%s)-$name"
                    write_incident "$name" "$kind:$target" "$cur" "$incident_id"
                    mark_diag_done "$name"
                    # 异步跑 diagnose（不阻塞下次 cron）
                    nohup "$DC_ROOT/scripts-watchdog/diagnose.sh" "$incident_id" "$name" >/dev/null 2>&1 &
                    disown $! 2>/dev/null || true
                fi
            elif in_cooldown "$name"; then
                emit_event "$name" "$kind:$target" "$prev_status" "$cur" "cooldown_skipped_diagnose"
            else
                incident_id="$(date +%s)-$name"
                write_incident "$name" "$kind:$target" "$cur" "$incident_id"
                mark_diag_done "$name"
                # 异步跑 diagnose（不阻塞下次 cron）
                nohup "$DC_ROOT/scripts-watchdog/diagnose.sh" "$incident_id" "$name" >/dev/null 2>&1 &
                disown $! 2>/dev/null || true
            fi
        elif [ "$cur_simple" = "ok" ] && [ "$prev_simple" = "fail" ]; then
            # fail → ok：写"已恢复"通知（不调 codex）
            /usr/bin/osascript -e "display notification \"$name 已恢复\" with title \"DC-Agent ✓\"" 2>/dev/null || true
        fi
    fi
done
