#!/bin/bash
# 收到 incident_id 后调 codex exec 自动诊断
# 由 dc-watchdog.sh 异步调用
#
# 输入：incident-<id>.json
# 输出：incident-<id>.md（codex 写的诊断报告）+ macOS 通知

set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/Users/dianchi/.local/bin"
export PYTHONPATH="/Users/dianchi/DC-Agent/dc_engines:/Users/dianchi/DC-Agent:${PYTHONPATH:-}"

INCIDENT_ID="${1:-}"
SERVICE="${2:-unknown}"
DC_ROOT="/Users/dianchi/DC-Agent"
WD_ROOT="$DC_ROOT/data/watchdog"
INCIDENT_DIR="$WD_ROOT/incidents"
ALERTS_LOG="$WD_ROOT/alerts.jsonl"
REPAIR_STATE="$WD_ROOT/repair_state.json"
REPAIR_EVENTS="$WD_ROOT/repairs.jsonl"

if [ -z "$INCIDENT_ID" ]; then
    echo "用法: $0 <incident_id> <service_name>" >&2
    exit 1
fi

SNAPSHOT="$INCIDENT_DIR/incident-${INCIDENT_ID}.json"
REPORT="$INCIDENT_DIR/incident-${INCIDENT_ID}.md"
REPAIR_PROPOSAL="$INCIDENT_DIR/incident-${INCIDENT_ID}.repair-proposal.json"
REPAIR_RESULT="$INCIDENT_DIR/incident-${INCIDENT_ID}.repair-result.json"
CODEX_BUNDLE="$INCIDENT_DIR/incident-${INCIDENT_ID}.codex-bundle.json"

if [ ! -f "$SNAPSHOT" ]; then
    echo "snapshot 不存在: $SNAPSHOT" >&2
    exit 1
fi

# 构造 prompt
PROMPT_FILE=$(mktemp /tmp/codex-prompt-XXXXXX)
cat > "$PROMPT_FILE" <<'PROMPT_HEAD'
你是 DC-Agent 系统的运维诊断助手。下面这份 JSON 是 watchdog 探测到的故障现场快照。

请输出一个符合给定 JSON Schema 的对象，同时包含 report_markdown 和 proposal。

report_markdown 是面向飞书技术群直接阅读的“技术分析结果报告”。要求：
- 重点给出判断、影响、建议，不要把原始错误代码、堆栈、长日志整段贴出来。
- 禁止使用 Markdown 代码围栏。
- “判断依据”只能用中文概括，最多引用 2 条很短的关键片段，每条不超过 120 字。
- 建议命令最多 3 条，只放必要命令；不要输出大段 shell 脚本。
- 如果无法确认根因，请明确写“最可能原因”和“还需补充确认项”，不要用日志堆砌代替判断。

必须包含以下 5 节：

## 1. 结论
1-2 句说清楚最可能的根因和当前判断。

## 2. 影响范围
说明哪些服务/功能受影响，哪些可能只是监控级异常。

## 3. 判断依据（摘要）
用人能读懂的话概括 snapshot 中支持判断的信号，不贴大段日志。

## 4. 处理建议
给出 2-4 条按优先级排列的处理动作；必要时包含不超过 3 条短命令。

## 5. 严重度
P0（业务全挂）/ P1（部分功能）/ P2（监控级，业务无感）

────────────── snapshot 开始 ──────────────
PROMPT_HEAD

cat "$SNAPSHOT" >> "$PROMPT_FILE"

cat >> "$PROMPT_FILE" <<'PROMPT_TAIL'

────────────── snapshot 结束 ──────────────

report_markdown 中直接写 Markdown，不要寒暄、不要用代码围栏包裹、不要粘贴大段原始错误代码。

proposal 是同一次分析产生的结构化修复建议。action_id 只能选择：
- observe_only：故障可能已在恢复、证据不足，或现在不应动作。
- restart_managed_service：仅当进程或监听端点不可用，并且一次安全重启最可能恢复。
- manual_intervention：代码、配置、权限、数据、依赖或未知根因，需要人工处理。

proposal 禁止包含命令、脚本、参数或文件修改。confidence 必须反映证据强度；
risk_hint 只是建议，最终风险由确定性策略引擎重新计算。

整体只输出符合 Schema 的 JSON 对象。
PROMPT_TAIL

# 调 codex
# -o, --output-last-message 让 codex 把最终答案直接写文件，
#   banner / reasoning trace / "tokens used" 都不进 REPORT
RAW_LOG="$INCIDENT_DIR/incident-${INCIDENT_ID}.codex.log"
if ! python3 -m dc_engines.codex_capability authorize \
    --capability incident_diagnosis \
    --authorized-by deterministic_controller >/dev/null; then
    cat > "$REPORT" <<EOF
# Codex 未获执行授权，仅 snapshot 保留

高级执行器策略拒绝了本次自动诊断。

snapshot：\`$SNAPSHOT\`
EOF
    STATUS="codex_policy_denied"
elif command -v codex >/dev/null 2>&1; then
    BUNDLE_SCHEMA_FILE=$(mktemp /tmp/codex-repair-bundle-schema-XXXXXX)
    "$DC_ROOT/.venv/bin/python" "$DC_ROOT/scripts-watchdog/repair_engine.py" \
        bundle-schema > "$BUNDLE_SCHEMA_FILE"
    if /opt/homebrew/bin/codex exec \
        --ignore-user-config \
        --ephemeral \
        -C /private/tmp \
        --disable apps \
        --disable plugins \
        --disable multi_agent \
        --skip-git-repo-check \
        --color never \
        --sandbox read-only \
        --model gpt-5.6-sol \
        -c 'model_provider="openai"' \
        -c 'model_reasoning_effort="max"' \
        --output-schema "$BUNDLE_SCHEMA_FILE" \
        -o "$CODEX_BUNDLE" \
        < "$PROMPT_FILE" > "$RAW_LOG" 2>&1; then
        if "$DC_ROOT/.venv/bin/python" "$DC_ROOT/scripts-watchdog/repair_engine.py" \
            split-bundle \
            --bundle "$CODEX_BUNDLE" \
            --report "$REPORT" \
            --proposal "$REPAIR_PROPOSAL"; then
            STATUS="ok"
        else
            STATUS="codex_invalid_output"
            cat > "$REPORT" <<EOF
# Codex 结构化输出无效

Codex 已返回结果，但本地确定性拆分校验失败，未进入自动修复。

bundle：\`$CODEX_BUNDLE\`
EOF
        fi
    else
        STATUS="codex_failed"
        # Codex 失败时 bundle 可能不存在，把 raw log 当报告用。
        [ ! -s "$REPORT" ] && cp "$RAW_LOG" "$REPORT"
    fi
    rm -f "$BUNDLE_SCHEMA_FILE"
else
    cat > "$REPORT" <<EOF
# Codex 不可用，仅 snapshot 保留

codex CLI 没装或不在 PATH 里，无法自动诊断。

snapshot：\`$SNAPSHOT\`
EOF
    STATUS="codex_missing"
fi

rm -f "$PROMPT_FILE"

# Codex only proposes a bounded action ID. The deterministic repair engine
# recomputes risk, rechecks current health, and maps the ID to a fixed command.
REPAIR_STATUS="not_generated"
REPAIR_SUMMARY="未生成修复建议"
if [ "$STATUS" = "ok" ]; then
    if "$DC_ROOT/.venv/bin/python" "$DC_ROOT/scripts-watchdog/repair_engine.py" \
        apply \
        --incident "$SNAPSHOT" \
        --proposal "$REPAIR_PROPOSAL" \
        --state "$REPAIR_STATE" \
        --result "$REPAIR_RESULT" \
        --events "$REPAIR_EVENTS" \
        --dc-root "$DC_ROOT" >/dev/null; then
        REPAIR_SUMMARY=$(python3 - "$REPAIR_RESULT" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    result = json.load(stream)
status = result.get("status", "unknown")
reason = result.get("reason", "unknown")
verification = result.get("verification", {})
detail = verification.get("detail") if isinstance(verification, dict) else None
rollback = result.get("rollback")
parts = [f"{status}: {reason}"]
if detail:
    parts.append(f"verify={detail}")
if rollback:
    parts.append(f"rollback={rollback}")
print("; ".join(parts))
PY
)
        REPAIR_STATUS=$(python3 -c "import json; print(json.load(open('$REPAIR_RESULT')).get('status', 'unknown'))")
    else
        REPAIR_STATUS="engine_failed"
        REPAIR_SUMMARY="确定性修复引擎执行失败"
    fi
fi

# 抽 codex 报告头一句话当通知 body
SUMMARY=$(grep -E "^##? 1\.|^## 1\.|结论|根因|Root Cause" "$REPORT" -A 2 2>/dev/null | head -3 | tr '\n' ' ' | head -c 200)
[ -z "$SUMMARY" ] && SUMMARY=$(head -3 "$REPORT" | tr '\n' ' ' | head -c 200)

# 双通道告警：macOS + 飞书（通过 dc_engines.alert_channel 引擎）
TITLE="🚨 DC-Agent: $SERVICE 异常"

# 1) macOS 通知（本机弹窗）
/usr/bin/osascript <<EOF 2>/dev/null || true
display notification "$(echo "$SUMMARY" | sed 's/"/\\"/g')" with title "$TITLE" subtitle "已 codex 自动诊断 → incident-${INCIDENT_ID}.md"
EOF

# 2) 飞书推送（通过 alert_channel 引擎 · 让你出公司也能收到）
CUR_STATUS=$(python3 -c "import json; print(json.load(open('$INCIDENT_DIR/incident-${INCIDENT_ID}.json'))['cur_status'])" 2>/dev/null || echo "fail")
REPORT_PREVIEW=$(python3 - "$REPORT" <<'PY'
from __future__ import annotations

import re
import sys
from pathlib import Path

report_path = Path(sys.argv[1])
try:
    raw = report_path.read_text(encoding="utf-8", errors="ignore")
except OSError:
    raw = ""

skip_prefixes = (
    "Traceback ",
    "File \"",
    "at ",
    "Caused by:",
    "Error:",
    "Exception:",
)
kept: list[str] = []
in_fence = False

for line in raw.splitlines():
    stripped = line.strip()
    if stripped.startswith(chr(96) * 3):
        in_fence = not in_fence
        continue
    if in_fence:
        continue
    if stripped.startswith(">"):
        continue
    if stripped.startswith(skip_prefixes):
        continue
    if re.search(r"\b[A-Za-z_][A-Za-z0-9_.]*(Error|Exception)\b", stripped):
        continue
    stripped = stripped.replace(chr(96), "")
    if len(stripped) > 220:
        stripped = stripped[:217] + "..."
    kept.append(stripped)

text = "\n".join(kept).strip()
if len(text) > 1200:
    text = text[:1197].rstrip() + "..."

print(text or "诊断报告生成失败；请查看本地 incident 文件。")
PY
)
ALERT_BODY="**服务**: \`$SERVICE\`
**状态**: $CUR_STATUS
**Incident**: \`incident-${INCIDENT_ID}.md\`
**自动修复**: $REPAIR_SUMMARY

**技术分析结果**:
$REPORT_PREVIEW

完整报告 + 接力诊断脚本见 \`data/watchdog/incidents/incident-${INCIDENT_ID}-followup.sh\`"

ALERT_ACTION_ARGS=()
if [ "$REPAIR_STATUS" = "review_required" ]; then
    ALERT_ACTION_ARGS=(
        --action-url "${DC_AGENT_DASHBOARD_URL:-http://127.0.0.1:6185/}"
        --action-label "打开 Dashboard 审核"
    )
fi
if "$DC_ROOT/.venv/bin/python" -m dc_engines.alert_channel \
    --title "$TITLE" \
    --body "$ALERT_BODY" \
    --level critical \
    "${ALERT_ACTION_ARGS[@]}" \
    --quiet 2>>"$INCIDENT_DIR/alert_channel.log"; then
    if [ "$REPAIR_STATUS" = "review_required" ]; then
        "$DC_ROOT/.venv/bin/python" "$DC_ROOT/scripts-watchdog/repair_engine.py" \
            mark-review-notification \
            --state "$REPAIR_STATE" \
            --incident "$INCIDENT_ID" \
            --stage created >/dev/null || true
    fi
else
    echo "[diagnose] alert_channel 推送失败（不阻塞）" >>"$INCIDENT_DIR/alert_channel.log"
fi

# 写 follow-up 命令文件，user copy-paste 一行就能让 Claude Code 接力
FOLLOWUP_FILE="$INCIDENT_DIR/incident-${INCIDENT_ID}-followup.sh"
cat > "$FOLLOWUP_FILE" <<EOF
#!/bin/bash
# Claude Code TUI 接力诊断 incident-${INCIDENT_ID}
# 复制下面这行到终端跑：
#
#   cd $DC_ROOT && claude "读 $REPORT 帮我深入诊断 $SERVICE 故障并给修复方案"
#
cd "$DC_ROOT" && claude "读 $REPORT 帮我深入诊断 $SERVICE 故障并给修复方案"
EOF
chmod +x "$FOLLOWUP_FILE"

# 写 alerts.jsonl
python3 -c "
import json
entry = {
    'ts': '$(date -u +%Y-%m-%dT%H:%M:%SZ)',
    'service': '$SERVICE',
    'event': 'diagnose_done',
    'incident_id': '$INCIDENT_ID',
    'codex_status': '$STATUS',
    'repair_status': '$REPAIR_STATUS',
    'report': '$REPORT',
    'codex_bundle': '$CODEX_BUNDLE',
    'repair_proposal': '$REPAIR_PROPOSAL',
    'repair_result': '$REPAIR_RESULT',
    'followup': '$FOLLOWUP_FILE',
}
open('$ALERTS_LOG','a').write(json.dumps(entry, ensure_ascii=False) + '\n')
"
