#!/bin/bash
# 巅池-技术 夜间任务（cron 01:00 北京时间触发 = 美西 PT 10:00 PDT）
#
# 两阶段：
#   阶段 A：Antigravity CLI (`agy -p`) 抓硅谷四大 AI 当日动态 → raw_news.md
#   阶段 B：Antigravity CLI (`agy -p`) 读 raw_news.md，做分析 + 学习 + 巡检 → report.md
#
# 失败不阻断：阶段 A 失败也跑阶段 B（让 agy 知道"今天没新闻"也能学习+巡检）
#
# 不做的事：
#   - 不发飞书（那是 09:30 reporter.py 的事）
#   - 不改任何源代码（prompt 里硬约束 agy 只读）

set -uo pipefail

# cron 跑时 PATH / env 都空，显式设
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/Users/dianchi/.local/bin"

# 飞书凭证 + 其他 secret（yaml 用 ${FEISHU_APP_SECRET} 占位）
[ -f "$HOME/.dc-agent.env" ] && set -a && . "$HOME/.dc-agent.env" && set +a

DC_ROOT="/Users/dianchi/DC-Agent"
PLUGIN_DIR="$DC_ROOT/data/plugins/dianchi_tech"
DATA_ROOT="$DC_ROOT/data/dianchi_tech"
DATE="$(date +%Y-%m-%d)"
DAY_DIR="$DATA_ROOT/$DATE"
LOG="$DATA_ROOT/cron.log"
VENV_PY="$DC_ROOT/.venv/bin/python"
ANALYSIS_MAX_ATTEMPTS="${DIANCHI_TECH_ANALYSIS_MAX_ATTEMPTS:-2}"
ANALYSIS_HARD_TIMEOUT_SECONDS="${DIANCHI_TECH_ANALYSIS_HARD_TIMEOUT_SECONDS:-2400}"
ANALYSIS_RUNNER_TIMEOUT_SECONDS="${DIANCHI_TECH_ANALYSIS_RUNNER_TIMEOUT_SECONDS:-2100}"

mkdir -p "$DAY_DIR"
touch "$LOG"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [night] $*" | tee -a "$LOG"; }

# macOS 没自带 timeout（GNU coreutils）；用 perl fork+alarm 模拟。
#
# 注意 1：cron 环境继承的 LC_CTYPE=C.UTF-8 会让 perl panic 退出 9，必须 unset。
# 注意 2：旧版用 `perl -e 'alarm shift; exec @ARGV'` 是错的——exec 后 perl
#         进程被目标进程替换，alarm 信号一并消失，timeout 形同虚设
#         （历史教训：2026-05-27 Claude 卡 7h23m 才被外部 SIGALRM 偶然杀掉）。
#         现在 perl 父进程保留，fork 子进程跑命令，超时由父进程负责 kill。
# 注意 3：子进程开新 process group（setpgrp），agy 这类会 spawn 子孙
#         的命令，TERM 整组才不会留孤儿。
# 注意 4：退出码遵循 GNU timeout 约定——超时返 124，否则透传子进程退出码。
#
# 用法：with_timeout SECONDS cmd args...
with_timeout() {
    local secs="$1"; shift
    LC_ALL=C LC_CTYPE=C LANG=C perl -e '
        use POSIX qw(setpgid);
        my $secs = shift;
        my $pid = fork();
        die "fork failed: $!" unless defined $pid;
        if ($pid == 0) {
            setpgid(0, 0);           # 新 process group，便于成组 kill
            exec { $ARGV[0] } @ARGV;
            exit 127;                # exec 失败才到这里
        }
        $SIG{ALRM} = sub {
            kill "-TERM", $pid;      # 负号 = 整个 process group
            sleep 5;
            kill "-KILL", $pid;
            exit 124;
        };
        alarm $secs;
        waitpid $pid, 0;
        alarm 0;
        my $status = $?;
        exit (($status & 127) ? 128 + ($status & 127) : ($status >> 8));
    ' "$secs" "$@"
}

log "=== 夜间任务启动 $DATE ==="

# ─────────── 阶段 A：searcher.py（agy 主 / Tavily+aihubmix 兜底）───────────
log "阶段 A：searcher.py 抓硅谷 AI 资讯（agy 主通道 + Tavily 兜底）"

AGY_START="$(date +%s)"

# 整体 8 分钟硬上限：agy 240s + 兜底 Tavily*4 + aihubmix ≈ 留出余量
if with_timeout 480 "$VENV_PY" "$PLUGIN_DIR/searcher.py" --date "$DATE" --out "$DAY_DIR/raw_news.md" \
        >> "$DAY_DIR/searcher_stdout.log" 2>&1; then
    AGY_EXIT=0
else
    AGY_EXIT=$?
fi

AGY_DUR=$(( $(date +%s) - AGY_START ))
log "阶段 A 结束 exit=$AGY_EXIT 耗时 ${AGY_DUR}s（0=agy主通道成功 / 1=兜底救场 / 2=全失败占位）"

# searcher 始终会写 raw_news.md（哪怕是占位），所以不需要再写兜底文件
if [ ! -s "$DAY_DIR/raw_news.md" ]; then
    log "⚠️  searcher.py 异常退出且未写文件，写占位"
    echo "# 硅谷 AI 资讯 $DATE\n\n⚠️ searcher.py 异常（exit=$AGY_EXIT）。详见 searcher_stdout.log。" > "$DAY_DIR/raw_news.md"
fi

# ─────────── 阶段 B：agy 分析 + 学习 + 巡检 ───────────
log "阶段 B：agy 分析 + 学习 + 巡检（max_attempts=$ANALYSIS_MAX_ATTEMPTS hard_timeout=${ANALYSIS_HARD_TIMEOUT_SECONDS}s runner_timeout=${ANALYSIS_RUNNER_TIMEOUT_SECONDS}s）"

ANALYSIS_START="$(date +%s)"
ANALYSIS_EXIT=1
ANALYSIS_ATTEMPTS_JSON=""
ANALYSIS_ATTEMPT_COUNT=0

# 在 DC_ROOT 下跑，agy 能读到 git / data / 源码
cd "$DC_ROOT"

# analyzer.py 负责读取 raw_news.md、调用 agy_runner、写 report.md 和 learning_log.json。
# 阶段 B 是无人值守高风险段：历史上 2026-05-27 曾卡 7h+ 后降级。
# 因此这里做两层保护：
#   1. 单次硬超时，超时后杀整个进程组。
#   2. 自动重试 1 次，仍失败才交给早上的 reporter 发降级通知。
: > "$DAY_DIR/analysis_stdout.log"
attempt=1
while [ "$attempt" -le "$ANALYSIS_MAX_ATTEMPTS" ]; do
    ATTEMPT_START="$(date +%s)"
    log "阶段 B attempt=$attempt/$ANALYSIS_MAX_ATTEMPTS 开始"
    {
        echo "===== analysis attempt $attempt started $(date '+%Y-%m-%d %H:%M:%S') ====="
    } >> "$DAY_DIR/analysis_stdout.log"

    if with_timeout "$ANALYSIS_HARD_TIMEOUT_SECONDS" "$VENV_PY" "$PLUGIN_DIR/analyzer.py" --date "$DATE" --day-dir "$DAY_DIR" --timeout "$ANALYSIS_RUNNER_TIMEOUT_SECONDS" \
            >> "$DAY_DIR/analysis_stdout.log" 2>&1; then
        ATTEMPT_EXIT=0
    else
        ATTEMPT_EXIT=$?
    fi

    ATTEMPT_DUR=$(( $(date +%s) - ATTEMPT_START ))
    ANALYSIS_ATTEMPT_COUNT="$attempt"
    ATTEMPT_KIND="failed"
    if [ "$ATTEMPT_EXIT" -eq 0 ]; then
        ATTEMPT_KIND="success"
    elif [ "$ATTEMPT_EXIT" -eq 124 ]; then
        ATTEMPT_KIND="timeout"
    fi
    log "阶段 B attempt=$attempt 结束 exit=$ATTEMPT_EXIT kind=$ATTEMPT_KIND 耗时 ${ATTEMPT_DUR}s"
    {
        echo "===== analysis attempt $attempt ended exit=$ATTEMPT_EXIT kind=$ATTEMPT_KIND duration=${ATTEMPT_DUR}s ====="
    } >> "$DAY_DIR/analysis_stdout.log"

    ATTEMPT_JSON=$(printf '{"attempt":%s,"exit":%s,"kind":"%s","duration_seconds":%s}' "$attempt" "$ATTEMPT_EXIT" "$ATTEMPT_KIND" "$ATTEMPT_DUR")
    if [ -z "$ANALYSIS_ATTEMPTS_JSON" ]; then
        ANALYSIS_ATTEMPTS_JSON="$ATTEMPT_JSON"
    else
        ANALYSIS_ATTEMPTS_JSON="$ANALYSIS_ATTEMPTS_JSON,$ATTEMPT_JSON"
    fi

    ANALYSIS_EXIT="$ATTEMPT_EXIT"
    if [ "$ATTEMPT_EXIT" -eq 0 ] && [ -s "$DAY_DIR/report.md" ]; then
        break
    fi

    attempt=$((attempt + 1))
    if [ "$attempt" -le "$ANALYSIS_MAX_ATTEMPTS" ]; then
        log "阶段 B 将重试 attempt=$attempt/$ANALYSIS_MAX_ATTEMPTS"
    fi
done

ANALYSIS_DUR=$(( $(date +%s) - ANALYSIS_START ))
log "阶段 B 结束 exit=$ANALYSIS_EXIT 耗时 ${ANALYSIS_DUR}s"

# ─────────── 写 run.json ───────────
cat > "$DAY_DIR/run.json" <<EOF
{
  "date": "$DATE",
  "started_at": "$(date -u -r $AGY_START +%Y-%m-%dT%H:%M:%SZ)",
  "ended_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "agy": { "exit": $AGY_EXIT, "duration_seconds": $AGY_DUR },
  "analysis": {
    "provider": "agy",
    "exit": $ANALYSIS_EXIT,
    "duration_seconds": $ANALYSIS_DUR,
    "max_attempts": $ANALYSIS_MAX_ATTEMPTS,
    "attempt_count": $ANALYSIS_ATTEMPT_COUNT,
    "hard_timeout_seconds": $ANALYSIS_HARD_TIMEOUT_SECONDS,
    "runner_timeout_seconds": $ANALYSIS_RUNNER_TIMEOUT_SECONDS,
    "retried": $([ "$ANALYSIS_ATTEMPT_COUNT" -gt 1 ] && echo true || echo false),
    "attempts": [$ANALYSIS_ATTEMPTS_JSON]
  },
  "raw_news_bytes": $(stat -f%z "$DAY_DIR/raw_news.md" 2>/dev/null || echo 0),
  "report_bytes": $(stat -f%z "$DAY_DIR/report.md" 2>/dev/null || echo 0)
}
EOF

if [ -s "$DAY_DIR/report.md" ]; then
    log "阶段 C：report_guard 校验数据源与运行事实"
    if with_timeout 60 "$VENV_PY" "$PLUGIN_DIR/report_guard.py" --date "$DATE" --day-dir "$DAY_DIR" \
            >> "$DAY_DIR/report_guard_stdout.log" 2>&1; then
        log "✓ report_guard 校验完成"
    else
        GUARD_EXIT=$?
        log "❌ report_guard 失败 exit=$GUARD_EXIT——报告事实未校验，明早 reporter 会再次尝试"
    fi
fi

if [ ! -s "$DAY_DIR/report.md" ]; then
    log "❌ report.md 不存在或为空——明早 reporter 会推一条降级通知"
    exit 1
fi

log "✓ 夜间任务完成，report.md $(wc -c < "$DAY_DIR/report.md") bytes"
exit 0
