#!/bin/bash
# ============================================================
# 百度网盘 → NAS 增量同步脚本
# 策略：对比远端 mtime，没变化跳过，有更新才重新下载
#       网络超时自动重试最多 MAX_RETRIES 次
# 用法：
#   bash sync_baidu_to_nas.sh              # 同步所有文件夹
#   bash sync_baidu_to_nas.sh "封面模板&尾版"  # 单文件夹
#   bash sync_baidu_to_nas.sh --force      # 强制全量重下
# ============================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NAS_MOUNT="/Users/dianchi/nas_kb"
NAS_SYNC_DIR="$NAS_MOUNT/柳汽"           # 直接写入 NAS 柳汽根目录
MTIME_CACHE="$SCRIPT_DIR/logs/sync_mtime_cache"
LOG_FILE="$SCRIPT_DIR/logs/sync_$(date +%Y%m%d).log"
LOCK_DIR="$SCRIPT_DIR/logs/baidu_sync.lock"
MAX_RETRIES=5          # 网络超时最多重试次数
RETRY_DELAY=30         # 每次重试前等待秒数

REMOTE_FOLDERS=(
    "柳汽/封面模板&尾版"
    "柳汽/柳汽Q1季度素材成片汇总"
    "柳汽/柳汽后市场新媒体运营视频"
)

FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1 && shift

# ──────────────────────────────────────────────────────────────
mkdir -p "$SCRIPT_DIR/logs"
exec > >(tee -a "$LOG_FILE") 2>&1

log()  { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }
ok()   { echo "[$(date '+%Y-%m-%d %H:%M:%S')] ✓ $*"; }
fail() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] ✗ $*" >&2; }
skip() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] - SKIP $*"; }

cleanup_lock() {
    rmdir "$LOCK_DIR" 2>/dev/null || true
}

acquire_lock() {
    if ! mkdir "$LOCK_DIR" 2>/dev/null; then
        skip "已有百度网盘 → NAS 同步任务在运行，本轮退出"
        exit 0
    fi
    trap cleanup_lock EXIT
}

nas_is_mounted() {
    mount | grep -q "on ${NAS_MOUNT} "
}

nas_is_healthy() {
    /usr/bin/smbutil statshares -a 2>/dev/null | grep -q "SERVER_NAME                   192.168.1.35"
}

# ── 前置检查 ──────────────────────────────────────────────────
preflight() {
    if ! command -v bdpan &>/dev/null; then
        fail "bdpan 未安装"; exit 1
    fi
    if ! nas_is_mounted || ! nas_is_healthy; then
        log "NAS 未挂载，尝试自动挂载..."
        bash "$SCRIPT_DIR/mount.sh" mount || { fail "NAS 挂载失败"; exit 1; }
        ok "NAS 挂载成功"
    fi
    if ! bdpan whoami &>/dev/null; then
        fail "百度网盘未登录，请运行: bash ~/.workbuddy/skills/百度网盘/scripts/login.sh"
        exit 1
    fi
}

# ── 获取远端文件夹 mtime（处理 & → & 的 JSON 转义）────────
get_remote_mtime() {
    local remote_path="$1"
    local parent="${remote_path%/*}"
    local name="${remote_path##*/}"
    local name_escaped
    name_escaped=$(echo "$name" | sed 's/&/\\u0026/g')
    bdpan ls "$parent" --json 2>/dev/null \
        | grep -A5 "\"server_filename\": \"$name_escaped\"" \
        | grep '"server_mtime"' \
        | head -1 \
        | sed 's/.*: "//;s/".*//'
}

# ── mtime 缓存读写 ─────────────────────────────────────────────
read_cached_mtime() {
    grep "^${1}=" "$MTIME_CACHE" 2>/dev/null | cut -d= -f2-
}

write_cached_mtime() {
    touch "$MTIME_CACHE"
    grep -v "^${1}=" "$MTIME_CACHE" > "${MTIME_CACHE}.tmp" && \
        mv "${MTIME_CACHE}.tmp" "$MTIME_CACHE"
    echo "${1}=${2}" >> "$MTIME_CACHE"
}

# ── 带重试的下载 ───────────────────────────────────────────────
download_with_retry() {
    local remote_path="$1"
    local local_dest="$2"
    local attempt=1

    while [ $attempt -le $MAX_RETRIES ]; do
        if bdpan download "$remote_path" "$local_dest"; then
            return 0
        fi
        if [ $attempt -lt $MAX_RETRIES ]; then
            log "下载失败，第 $attempt 次重试，等待 ${RETRY_DELAY}s..."
            sleep $RETRY_DELAY
        fi
        (( attempt++ ))
    done
    return 1
}

# ── 同步单个文件夹 ─────────────────────────────────────────────
sync_one() {
    local remote_path="$1"
    local folder_name="${remote_path##*/}"
    local local_dest="$NAS_SYNC_DIR/$folder_name"
    local cache_key
    cache_key=$(echo "$remote_path" | tr '/' '_' | tr ' &' '__')

    # mtime 检查
    local remote_mtime
    remote_mtime=$(get_remote_mtime "$remote_path")

    if [ -n "$remote_mtime" ] && [ "$FORCE" -eq 0 ]; then
        local cached_mtime
        cached_mtime=$(read_cached_mtime "$cache_key")
        if [ "$remote_mtime" = "$cached_mtime" ] && [ -d "$local_dest" ]; then
            skip "$folder_name（远端未更新，上次同步: $cached_mtime）"
            return 0
        fi
    fi

    mkdir -p "$local_dest"
    log "下载: $remote_path → $local_dest"

    if download_with_retry "$remote_path" "$local_dest"; then
        ok "$folder_name 同步完成"
        [ -n "$remote_mtime" ] && write_cached_mtime "$cache_key" "$remote_mtime"
        return 0
    else
        fail "$folder_name 同步失败（已重试 $MAX_RETRIES 次）"
        return 1
    fi
}

# ── 主流程 ────────────────────────────────────────────────────
main() {
    acquire_lock
    log "=================================================="
    log "百度网盘 → NAS 同步开始$([ $FORCE -eq 1 ] && echo '（强制全量）' || true)"
    log "目标: $NAS_SYNC_DIR"
    log "=================================================="

    preflight
    mkdir -p "$NAS_SYNC_DIR"

    local ok_count=0 fail_count=0 skip_count=0

    for folder in "${REMOTE_FOLDERS[@]}"; do
        if sync_one "$folder"; then
            # 判断是跳过还是成功
            grep -q "SKIP $( echo "${folder##*/}" )" "$LOG_FILE" 2>/dev/null && \
                (( skip_count++ )) || (( ok_count++ ))
        else
            (( fail_count++ ))
        fi
    done

    log "=================================================="
    log "结束 | 同步: $ok_count | 跳过: $skip_count | 失败: $fail_count"
    log "=================================================="
    [ "$fail_count" -gt 0 ] && exit 1
    exit 0
}

# ── 单文件夹模式 ──────────────────────────────────────────────
if [ $# -gt 0 ] && [ "${1}" != "--force" ]; then
    acquire_lock
    preflight
    mkdir -p "$NAS_SYNC_DIR"
    sync_one "柳汽/$1"
else
    main
fi
