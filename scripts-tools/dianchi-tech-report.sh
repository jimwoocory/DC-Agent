#!/bin/bash
# 巅池-技术 09:30 汇报（cron 09:30 北京时间）
# 读夜间产出的 report.md → 飞书私聊蔡挺 + 自动建 wiki 子页

set -uo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/Users/dianchi/.local/bin"

# 加载 secret（DIANCHI_TECH_APP_SECRET 等）
[ -f "$HOME/.dc-agent.env" ] && set -a && . "$HOME/.dc-agent.env" && set +a

DC_ROOT="/Users/dianchi/DC-Agent"
PLUGIN_DIR="$DC_ROOT/data/plugins/dianchi_tech"
DATA_ROOT="$DC_ROOT/data/dianchi_tech"
LOG="$DATA_ROOT/cron.log"
CONF_FILE="$DC_ROOT/data/config/dianchi_tech_config.json"

VENV_PY="$DC_ROOT/.venv/bin/python"

mkdir -p "$DATA_ROOT"
touch "$LOG"
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [report] $*" | tee -a "$LOG"; }

log "=== 汇报任务启动 $(date +%Y-%m-%d) ==="

# 从 plugin config 读 union_id / wiki space；config 被擦时用 hardcode fallback
# （AstrBot plugin reload 偶尔会用 dashboard 空字段覆盖 config，这里兜底）
CAI_UNION_ID_FALLBACK="on_d02f744ffca7d68eac1afee00d7edb71"
CAI_UNION_ID=""
WIKI_SPACE="日常任务报告"
if [ -f "$CONF_FILE" ]; then
    CAI_UNION_ID=$("$VENV_PY" -c "import json; print(json.load(open('$CONF_FILE')).get('cai_ting_union_id','') or '')" 2>/dev/null || echo "")
    WIKI_SPACE=$("$VENV_PY" -c "import json; print(json.load(open('$CONF_FILE')).get('wiki_space_name','日常任务报告'))" 2>/dev/null || echo "日常任务报告")
fi

if [ -z "$CAI_UNION_ID" ]; then
    log "⚠️ config 里 cai_ting_union_id 空，用 hardcode fallback $CAI_UNION_ID_FALLBACK"
    CAI_UNION_ID="$CAI_UNION_ID_FALLBACK"
fi

cd "$DC_ROOT"

if "$VENV_PY" "$PLUGIN_DIR/reporter.py" \
        --union-id "$CAI_UNION_ID" \
        --space "$WIKI_SPACE" \
        >> "$LOG" 2>&1; then
    log "✓ 汇报成功"
    exit 0
else
    code=$?
    log "❌ 汇报失败 exit=$code"
    exit $code
fi
