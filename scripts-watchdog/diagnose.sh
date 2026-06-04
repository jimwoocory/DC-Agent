#!/bin/bash
# 收到 incident_id 后调 codex exec 自动诊断
# 由 dc-watchdog.sh 异步调用
#
# 输入：incident-<id>.json
# 输出：incident-<id>.md（codex 写的诊断报告）+ macOS 通知

set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/Users/dianchi/.local/bin"

INCIDENT_ID="${1:-}"
SERVICE="${2:-unknown}"
DC_ROOT="/Users/dianchi/DC-Agent"
WD_ROOT="$DC_ROOT/data/watchdog"
INCIDENT_DIR="$WD_ROOT/incidents"
ALERTS_LOG="$WD_ROOT/alerts.jsonl"

if [ -z "$INCIDENT_ID" ]; then
    echo "用法: $0 <incident_id> <service_name>" >&2
    exit 1
fi

SNAPSHOT="$INCIDENT_DIR/incident-${INCIDENT_ID}.json"
REPORT="$INCIDENT_DIR/incident-${INCIDENT_ID}.md"

if [ ! -f "$SNAPSHOT" ]; then
    echo "snapshot 不存在: $SNAPSHOT" >&2
    exit 1
fi

# 构造 prompt
PROMPT_FILE=$(mktemp /tmp/codex-prompt-XXXXXX)
cat > "$PROMPT_FILE" <<'PROMPT_HEAD'
你是 DC-Agent 系统的运维诊断助手。下面这份 JSON 是 watchdog 探测到的故障现场快照。

请输出一份“技术分析结果报告”，面向飞书技术群直接阅读。要求：
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

请直接输出 Markdown，不要寒暄、不要用 Markdown 代码围栏包裹、不要粘贴大段原始错误代码。
PROMPT_TAIL

# 调 codex
# -o, --output-last-message 让 codex 把最终答案直接写文件，
#   banner / reasoning trace / "tokens used" 都不进 REPORT
RAW_LOG="$INCIDENT_DIR/incident-${INCIDENT_ID}.codex.log"
if command -v codex >/dev/null 2>&1; then
    if /opt/homebrew/bin/codex exec \
        --skip-git-repo-check \
        --color never \
        -o "$REPORT" \
        < "$PROMPT_FILE" > "$RAW_LOG" 2>&1; then
        STATUS="ok"
    else
        STATUS="codex_failed"
        # codex 失败时 -o 文件可能不存在，把 raw log 当报告用
        [ ! -s "$REPORT" ] && cp "$RAW_LOG" "$REPORT"
    fi
else
    cat > "$REPORT" <<EOF
# Codex 不可用，仅 snapshot 保留

codex CLI 没装或不在 PATH 里，无法自动诊断。

snapshot：\`$SNAPSHOT\`
EOF
    STATUS="codex_missing"
fi

rm -f "$PROMPT_FILE"

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

**技术分析结果**:
$REPORT_PREVIEW

完整报告 + 接力诊断脚本见 \`data/watchdog/incidents/incident-${INCIDENT_ID}-followup.sh\`"

"$DC_ROOT/.venv/bin/python" -m dc_engines.alert_channel \
    --title "$TITLE" \
    --body "$ALERT_BODY" \
    --level critical \
    --quiet 2>>"$INCIDENT_DIR/alert_channel.log" || \
    echo "[diagnose] alert_channel 推送失败（不阻塞）" >>"$INCIDENT_DIR/alert_channel.log"

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
    'report': '$REPORT',
    'followup': '$FOLLOWUP_FILE',
}
open('$ALERTS_LOG','a').write(json.dumps(entry, ensure_ascii=False) + '\n')
"
