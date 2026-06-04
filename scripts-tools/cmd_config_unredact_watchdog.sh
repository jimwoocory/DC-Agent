#!/bin/bash
# cmd_config.json 脱敏自愈 watchdog
#
# 触发：~/Library/LaunchAgents/io.dcagent.cmd-config-watchdog.plist 的 WatchPaths
#       监听 cmd_config.json 改动事件，文件每次写入都会调本脚本。
#
# 行为：
#   1. 检测 cmd_config 里有没有 ${REDACTED_*} 或 REDACTED_API_KEY 这种占位符
#   2. 有 → 立即抓当前进程快照（找凶手）+ 跑 restore_redacted_secrets.py 恢复 + 重启 AstrBot
#   3. 没有 → 静默退出（不形成循环）
#
# 凶手信息收集：当 redact 被检测到时，记录：
#   - 当前所有用户进程列表
#   - lsof on cmd_config.json（看谁还在持有 handle）
#   - 最近 5 分钟 dashboard / hermes-webui / launchd 活动
#
# 日志：/Users/dianchi/DC-Agent/logs/secret_redact_watchdog.log

set -u

CFG=/Users/dianchi/DC-Agent/data/cmd_config.json
LOG=/Users/dianchi/DC-Agent/logs/secret_redact_watchdog.log
RESTORE=/Users/dianchi/DC-Agent/scripts-tools/restore_redacted_secrets.py
VENV_PY=/Users/dianchi/DC-Agent/.venv/bin/python

# launchd 启动时 PATH 几乎是空的，显式设
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

mkdir -p "$(dirname "$LOG")"

TS=$(date '+%Y-%m-%d %H:%M:%S')

# 静默退出条件：文件不存在 / 无法读
[ -r "$CFG" ] || exit 0

# 精确检测：只关心业务关键字段被 redact 的情况
# - lark 平台 app_secret  (3 个机器人连接飞书必需)
# - aihubmix provider_source 的 key  (LLM 调用必需)
# - openai_embedding 的 embedding_api_key (知识库 embedding 必需)
# - codex/gemini OAuth provider_source 的 key 仍允许为空（OAuth 通过 ~/.codex/auth.json 等读 token，不在 cmd_config 存）
#
# dashboard password / 其他 webhook 类 secret 即使是 REDACTED 也不触发恢复
# （不影响 LLM/飞书业务功能，避免无效循环触发）
NEEDS_FIX=0

# lark app_secret 任一被 redact
if grep -qE '"app_secret":\s*"\$\{REDACTED|"app_secret":\s*"REDACTED_API_KEY' "$CFG" 2>/dev/null; then
    NEEDS_FIX=1
fi

# aihubmix provider_source key 被 redact (key 是 array, 取首个)
if grep -qE 'REDACTED_API_KEY[^"]*"' "$CFG" 2>/dev/null \
    && python3 -c "
import json,sys
try:
    d=json.load(open('$CFG',encoding='utf-8-sig'))
    for ps in d.get('provider_sources',[]):
        if ps.get('id')=='aihubmix':
            k=(ps.get('key') or [''])[0]
            if 'REDACTED' in k:
                sys.exit(0)
    sys.exit(1)
except Exception:
    sys.exit(1)
" 2>/dev/null; then
    NEEDS_FIX=1
fi

# embedding_api_key 被 redact
if grep -qE '"embedding_api_key":\s*"\$\{REDACTED|"embedding_api_key":\s*"REDACTED' "$CFG" 2>/dev/null; then
    NEEDS_FIX=1
fi

if [ "$NEEDS_FIX" = "0" ]; then
    # 业务关键字段都 OK —— 静默退出
    exit 0
fi

# ────────────── 凶手现场 ──────────────
{
    echo ""
    echo "================================================================"
    echo "🚨 $TS  cmd_config.json 被 redact 检测到"
    echo "================================================================"
    echo ""
    echo "--- 1. cmd_config 当前可疑字段 ---"
    grep -nE 'REDACTED|app_secret|api_key' "$CFG" 2>/dev/null | head -20
    echo ""
    echo "--- 2. 当前所有用户进程（按启动时间倒序，最近的最可疑）---"
    ps -A -o pid,start,command 2>/dev/null | grep -v '^ *PID' | sort -k2 -r | head -50
    echo ""
    echo "--- 3. lsof on cmd_config.json (当前哪些进程持有 handle)---"
    lsof "$CFG" 2>/dev/null || echo "  (没有进程当前持有 handle，写入者已 close)"
    echo ""
    echo "--- 4. 最近 5 分钟修改的关键文件（可能跟凶手相关）---"
    find /Users/dianchi/DC-Agent /Users/dianchi/Openclaw -type f -mmin -5 \
        ! -path '*/__pycache__/*' \
        ! -path '*/.venv/*' \
        ! -path '*/node_modules/*' \
        ! -path '*/_backup*' \
        ! -name '*.log' \
        ! -name '*.db*' 2>/dev/null | head -20
} >> "$LOG"

# ────────────── 自愈恢复 ──────────────
{
    echo ""
    echo "--- 5. 触发 restore_redacted_secrets.py 自愈 ---"
} >> "$LOG"

if [ -x "$VENV_PY" ] && [ -f "$RESTORE" ]; then
    cd /Users/dianchi/DC-Agent && "$VENV_PY" "$RESTORE" >> "$LOG" 2>&1
    RESTORE_EXIT=$?
    echo "    restore exit=$RESTORE_EXIT" >> "$LOG"
else
    echo "    ❌ venv python 或 restore 脚本不存在" >> "$LOG"
    RESTORE_EXIT=99
fi

# ────────────── 重启 AstrBot ──────────────
if [ "$RESTORE_EXIT" = "0" ]; then
    {
        echo ""
        echo "--- 6. 重启 AstrBot 让恢复的 secret 生效 ---"
    } >> "$LOG"
    launchctl kickstart -k "gui/$(id -u)/io.astrbot.bot" >> "$LOG" 2>&1
    echo "    AstrBot 已 kickstart" >> "$LOG"
fi

echo "" >> "$LOG"
echo "================================================================" >> "$LOG"

# 退出码 0 让 launchd 不视为失败
exit 0
