#!/bin/bash
# ============================================================
# NAS 挂载/卸载脚本
# 用法：
#   ./nas_sync/mount.sh mount    挂载 NAS
#   ./nas_sync/mount.sh unmount  卸载 NAS
#   ./nas_sync/mount.sh status   查看挂载状态
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="$SCRIPT_DIR/config.yaml"

# 读取 YAML 值（用 grep/sed，无需外部依赖）
# 支持格式：key.subkey
yaml_get() {
    local full_key="$1"
    local field="${full_key##*.}"
    # 匹配 "  field: value" 或 "field: value"，去掉注释和引号
    grep -m1 "^\s*${field}:\s" "$CONFIG" \
        | sed 's/^[^:]*:[[:space:]]*//' \
        | sed 's/[[:space:]]*#.*//' \
        | tr -d '"'"'" \
        | tr -d '\r'
}

NAS_PROTOCOL=$(yaml_get "nas.protocol")
NAS_HOST=$(yaml_get "nas.host")
NAS_SHARE=$(yaml_get "nas.share")
NAS_USER=$(yaml_get "nas.username")
NAS_PASSWORD_KEYCHAIN_KEY=$(yaml_get "nas.password_keychain_key")
NAS_MOUNT=$(yaml_get "nas.mount_point")
NFS_EXPORT=$(yaml_get "nas.nfs_export")
WEBDAV_PORT=$(yaml_get "nas.webdav_port")
WEBDAV_PATH=$(yaml_get "nas.webdav_path")
CREDS_FILE=""
cleanup() {
    if [[ -n "$CREDS_FILE" && -f "$CREDS_FILE" ]]; then
        rm -f "$CREDS_FILE"
    fi
}
trap cleanup EXIT

# 颜色输出
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()    { echo -e "${GREEN}[NAS]${NC} $*"; }
warn()    { echo -e "${YELLOW}[NAS]${NC} $*"; }
err()     { echo -e "${RED}[NAS]${NC} $*" >&2; }

read_keychain_password() {
    local service="$1"
    if [[ -z "$service" ]]; then
        err "config.yaml 缺少 nas.password_keychain_key"
        exit 1
    fi
    security find-generic-password -s "$service" -w
}

# ----------------------------------------------------------------
cmd_status() {
    if mount | grep -q "$NAS_MOUNT"; then
        info "已挂载: $NAS_MOUNT"
        mount | grep "$NAS_MOUNT"
    else
        warn "未挂载: $NAS_MOUNT"
    fi
}

# ----------------------------------------------------------------
cmd_mount() {
    if mount | grep -q "$NAS_MOUNT"; then
        info "已挂载，跳过。"
        return 0
    fi

    # 创建挂载点
    mkdir -p "$NAS_MOUNT"

    # 创建 NAS 目录结构（在挂载后执行）
    do_mount_and_init() {
        mkdir -p "$NAS_MOUNT/inbox"
        mkdir -p "$NAS_MOUNT/processed"
        mkdir -p "$NAS_MOUNT/archive"
        info "目录结构已初始化：inbox/ processed/ archive/"
    }

    case "$NAS_PROTOCOL" in
        smb)
            info "挂载 SMB: //$NAS_HOST/$NAS_SHARE → $NAS_MOUNT"
            if [[ "$(uname)" == "Darwin" ]]; then
                # macOS：从 Keychain 取密码，URL 中显式传给 mount_smbfs
                # （keychain 自动查找在某些项格式下不可靠，显式传更稳）
                local nas_pass
                nas_pass="$(read_keychain_password "$NAS_PASSWORD_KEYCHAIN_KEY")"
                if [[ -z "$nas_pass" ]]; then
                    err "Keychain 中未找到 $NAS_PASSWORD_KEYCHAIN_KEY 对应的密码"
                    exit 1
                fi
                mount_smbfs "//${NAS_USER}:${nas_pass}@${NAS_HOST}/${NAS_SHARE}" "$NAS_MOUNT"
            else
                # Linux：使用 mount.cifs
                if ! command -v mount.cifs &>/dev/null; then
                    err "请先安装 cifs-utils：sudo apt install cifs-utils"
                    exit 1
                fi
                local nas_pass
                nas_pass="$(read_keychain_password "$NAS_PASSWORD_KEYCHAIN_KEY")"
                CREDS_FILE="$(mktemp)"
                chmod 600 "$CREDS_FILE"
                {
                    printf 'username=%s\n' "$NAS_USER"
                    printf 'password=%s\n' "$nas_pass"
                } > "$CREDS_FILE"
                sudo mount.cifs "//$NAS_HOST/$NAS_SHARE" "$NAS_MOUNT" \
                    -o "credentials=$CREDS_FILE,iocharset=utf8,file_mode=0664,dir_mode=0775"
            fi
            ;;

        nfs)
            info "挂载 NFS: $NAS_HOST:$NFS_EXPORT → $NAS_MOUNT"
            if [[ "$(uname)" == "Darwin" ]]; then
                sudo mount -t nfs -o resvport "$NAS_HOST:$NFS_EXPORT" "$NAS_MOUNT"
            else
                sudo mount -t nfs "$NAS_HOST:$NFS_EXPORT" "$NAS_MOUNT"
            fi
            ;;

        webdav)
            info "挂载 WebDAV: http://$NAS_HOST:$WEBDAV_PORT$WEBDAV_PATH → $NAS_MOUNT"
            if [[ "$(uname)" == "Darwin" ]]; then
                # macOS：通过 Finder 挂载 WebDAV（或使用 davfs2）
                open "http://$NAS_USER@$NAS_HOST:$WEBDAV_PORT$WEBDAV_PATH"
                warn "macOS WebDAV 通过 Finder 挂载，挂载点可能不是 $NAS_MOUNT"
                warn "请在 Finder 中确认挂载后，修改 config.yaml 中的 mount_point"
            else
                if ! command -v mount.davfs &>/dev/null; then
                    err "请先安装 davfs2：sudo apt install davfs2"
                    exit 1
                fi
                read_keychain_password "$NAS_PASSWORD_KEYCHAIN_KEY" | sudo mount.davfs \
                    "http://$NAS_HOST:$WEBDAV_PORT$WEBDAV_PATH" "$NAS_MOUNT" \
                    -o username="$NAS_USER"
            fi
            ;;

        *)
            err "不支持的协议：$NAS_PROTOCOL（支持：smb | nfs | webdav）"
            exit 1
            ;;
    esac

    if mount | grep -q "$NAS_MOUNT"; then
        info "挂载成功！"
        do_mount_and_init
    else
        err "挂载失败，请检查 NAS 地址和账号密码。"
        exit 1
    fi
}

# ----------------------------------------------------------------
cmd_unmount() {
    if ! mount | grep -q "$NAS_MOUNT"; then
        warn "未挂载，跳过。"
        return 0
    fi

    info "卸载 $NAS_MOUNT ..."
    if [[ "$(uname)" == "Darwin" ]]; then
        diskutil unmount "$NAS_MOUNT" 2>/dev/null || umount "$NAS_MOUNT"
    else
        sudo umount "$NAS_MOUNT"
    fi
    info "已卸载。"
}

# ----------------------------------------------------------------
# 开机自动挂载（写入 launchd plist，macOS 专用）
cmd_autostart() {
    if [[ "$(uname)" != "Darwin" ]]; then
        warn "autostart 仅支持 macOS，Linux 请编辑 /etc/fstab 手动配置。"
        return 0
    fi

    local plist="$HOME/Library/LaunchAgents/com.jwbot.nas-mount.plist"
    cat > "$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.jwbot.nas-mount</string>
    <key>ProgramArguments</key>
    <array>
        <string>$SCRIPT_DIR/mount.sh</string>
        <string>mount</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
    <key>StandardOutPath</key>
    <string>$SCRIPT_DIR/mount.log</string>
    <key>StandardErrorPath</key>
    <string>$SCRIPT_DIR/mount.log</string>
</dict>
</plist>
PLIST

    launchctl load -w "$plist"
    info "已注册开机自动挂载：$plist"
}

# ----------------------------------------------------------------
case "${1:-status}" in
    mount)      cmd_mount      ;;
    unmount)    cmd_unmount    ;;
    status)     cmd_status     ;;
    autostart)  cmd_autostart  ;;
    *)
        echo "用法: $0 [mount|unmount|status|autostart]"
        exit 1
        ;;
esac
