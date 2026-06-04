#!/bin/bash
# AstrBot 重启后验证脚本
#
# 用途：每次 launchctl kickstart -k io.astrbot.bot 之后跑一次，
#       一眼看出当前启用的 AIHubMix LLM + 1 个 embedding 是不是全部能调通。
#
# 用法：
#   bash scripts-tools/verify_llm_after_restart.sh
#
# 退出码：
#   0 = 全部 ok
#   1 = 至少 1 个失败（脚本会标红显示）

set -u

DC_ROOT="/Users/dianchi/DC-Agent"
cd "$DC_ROOT"

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[0;33m'
NC='\033[0m'

ok()   { printf "  ${GREEN}✅${NC} %s\n" "$*"; }
fail() { printf "  ${RED}❌${NC} %s\n" "$*"; }
warn() { printf "  ${YELLOW}⚠️ ${NC} %s\n" "$*"; }

FAIL_COUNT=0

# ────────────────── 1. 端口 + 进程检查 ──────────────────
echo ""
echo "═══ 1. 基础服务存活检查 ═══"

for entry in "AstrBot:6185" "Hermes Gateway:8644" "AstrBot Response:8645" "Hermes WebUI:9119" "OpenClaw Watchdog:9120"; do
    name="${entry%:*}"
    port="${entry##*:}"
    if /usr/bin/nc -z -G 2 127.0.0.1 "$port" 2>/dev/null; then
        ok "$name :$port LISTEN"
    else
        fail "$name :$port 不通"
        FAIL_COUNT=$((FAIL_COUNT + 1))
    fi
done

# ────────────────── 2. AstrBot 加载了新配置 ──────────────────
echo ""
echo "═══ 2. AstrBot 是否加载了新 LLM 配置 ═══"

START_TIME=$(curl -s --max-time 3 http://127.0.0.1:6185/api/stat/start-time 2>/dev/null)
if [ -z "$START_TIME" ]; then
    fail "AstrBot API 无响应（可能没启动 / 端口被占）"
    FAIL_COUNT=$((FAIL_COUNT + 1))
else
    ok "AstrBot API 响应正常"
fi

# 读 cmd_config.json 看是否是清理后的 9 个 provider（5 个 chat + 3 个 Codex + embedding）
PROVIDER_COUNT=$(.venv/bin/python -c "
import json
d = json.load(open('data/cmd_config.json', encoding='utf-8-sig'))
print(len([p for p in d.get('provider', []) if p.get('enable', True)]))
" 2>/dev/null)

if [ "$PROVIDER_COUNT" = "8" ]; then
    ok "cmd_config.json 中启用的 provider 数 = 8（清理后预期值）"
else
    warn "cmd_config.json 中启用 provider 数 = $PROVIDER_COUNT（预期 8）—— 如果是旧值可能 AstrBot 还没读新配置"
fi

# ────────────────── 3. 当前 AIHubMix LLM 实际能调通 ──────────────────
echo ""
echo "═══ 3. 当前 AIHubMix LLM 直连测试（绕过 AstrBot 直调 aihubmix）═══"

API_KEY=$(.venv/bin/python -c "
import json
d = json.load(open('data/cmd_config.json', encoding='utf-8-sig'))
for s in d.get('provider_sources', []):
    if s.get('id') == 'aihubmix':
        keys = s.get('key') or s.get('keys') or []
        if isinstance(keys, list) and keys:
            print(keys[0])
            break
" 2>/dev/null)

if [ -z "$API_KEY" ]; then
    fail "找不到 aihubmix key（cmd_config.json 配置异常）"
    FAIL_COUNT=$((FAIL_COUNT + 1))
else
    ok "拿到 aihubmix key: ${API_KEY:0:10}***${API_KEY: -4}"

    for model in "gemini-3.5-flash" "gemini-3.1-pro-preview" "grok-4.3"; do
        resp=""
        for attempt in 1 2 3; do
            payload="{\"model\":\"$model\",\"messages\":[{\"role\":\"user\",\"content\":\"reply ok\"}],\"max_tokens\":50}"
            if [ "$model" = "gemini-3.5-flash" ]; then
                payload="{\"model\":\"$model\",\"messages\":[{\"role\":\"user\",\"content\":\"reply ok\"}]}"
            fi
            resp=$(curl -sS --max-time 45 https://aihubmix.com/v1/chat/completions \
                -H "Authorization: Bearer $API_KEY" \
                -H "Content-Type: application/json" \
                -d "$payload" 2>&1)
            if echo "$resp" | grep -qE '"id":"chatcmpl-|"content"'; then
                break
            fi
            sleep 1
        done

        # 认成功的标志：有 chatcmpl id（reasoning model 可能只有 reasoning_content 没 content）
        if echo "$resp" | grep -qE '"id":"chatcmpl-|"content"'; then
            ok "$model"
        else
            err=$(echo "${resp:-empty response after retries}" | head -c 180)
            fail "$model → $err"
            FAIL_COUNT=$((FAIL_COUNT + 1))
        fi
    done

    # embedding
    resp=$(curl -s --max-time 15 https://aihubmix.com/v1/embeddings \
        -H "Authorization: Bearer $API_KEY" \
        -H "Content-Type: application/json" \
        -d '{"model":"gemini-embedding-2-preview","input":"test"}' 2>&1)
    if echo "$resp" | grep -q '"embedding"'; then
        dim=$(echo "$resp" | .venv/bin/python -c "import sys,json; print(len(json.load(sys.stdin)['data'][0]['embedding']))" 2>/dev/null)
        ok "gemini-embedding-2-preview（维度=$dim）"
    else
        err=$(echo "$resp" | head -c 120)
        fail "embedding → $err"
        FAIL_COUNT=$((FAIL_COUNT + 1))
    fi
fi

# ────────────────── 4. 飞书 lark adapter 长连接是否重连 ──────────────────
echo ""
echo "═══ 4. 飞书 lark adapter 长连接状态 ═══"

LARK_LOG=$(tail -200 "$DC_ROOT/astrbot.log" 2>/dev/null | grep -E 'Loading IM platform adapter lark|Lark.*ERROR|Lark.*websocket.*conn' | tail -10)
if echo "$LARK_LOG" | grep -q 'Loading IM platform adapter lark'; then
    LARK_COUNT=$(echo "$LARK_LOG" | grep -c 'Loading IM platform adapter lark')
    ok "lark adapter 已加载（${LARK_COUNT} 条 log）"
    LATEST_LARK_ERR=$(tail -50 "$DC_ROOT/astrbot.log" 2>/dev/null | grep -E '\[Lark\].*ERROR' | tail -3)
    if [ -n "$LATEST_LARK_ERR" ]; then
        warn "最近有 lark 错误（重连过程中常见，5 分钟内自动恢复）:"
        echo "$LATEST_LARK_ERR" | sed 's/^/      /'
    fi
else
    warn "找不到 lark adapter 加载日志 —— 可能 AstrBot 还没启动完"
fi

# ────────────────── 5. dc-watchdog 11 项探针 ──────────────────
echo ""
echo "═══ 5. dc-watchdog 探针全景 ═══"

bash "$DC_ROOT/scripts-watchdog/dc-watchdog.sh" 2>/dev/null
WD_STATE=$(.venv/bin/python -c "
import json
d = json.load(open('data/watchdog/state.json'))
ok_count, fail_count = 0, 0
for k, v in d.items():
    if isinstance(v, dict):
        if v.get('status') == 'ok':
            ok_count += 1
        elif v.get('status', '').startswith('fail'):
            fail_count += 1
print(f'{ok_count}/{ok_count + fail_count}')
" 2>/dev/null)

OK_NUM=$(echo "$WD_STATE" | cut -d/ -f1)
TOTAL=$(echo "$WD_STATE" | cut -d/ -f2)
if [ "$OK_NUM" = "$TOTAL" ] && [ "$TOTAL" != "" ] && [ "$TOTAL" != "0" ]; then
    ok "$OK_NUM / $TOTAL 探针 ok"
else
    fail "$WD_STATE 探针通过（详情看 data/watchdog/state.json）"
    FAIL_COUNT=$((FAIL_COUNT + 1))
fi

# ────────────────── 总结 ──────────────────
echo ""
echo "═══════════════════════════════════════════════"
if [ "$FAIL_COUNT" = "0" ]; then
    printf "${GREEN}🎉 全部通过 · 可以拉群测试${NC}\n"
    exit 0
else
    printf "${RED}🚨 失败 %d 项 · 暂不要拉群${NC}\n" "$FAIL_COUNT"
    echo "   排查建议：先看 tail -50 astrbot.log"
    exit 1
fi
