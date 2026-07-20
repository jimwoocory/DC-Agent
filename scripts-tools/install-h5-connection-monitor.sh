#!/bin/bash
# Install or remove the privacy-safe H5 connection monitor LaunchAgent.

set -euo pipefail

DC_ROOT="/Users/dianchi/DC-Agent"
LABEL="com.dcagent.h5-connection-monitor"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PYTHON="$DC_ROOT/.venv/bin/python"
SCRIPT="$DC_ROOT/scripts-watchdog/h5_connection_monitor.py"
LOG_DIR="$DC_ROOT/data/watchdog/h5_connections"
DOMAIN="gui/$(id -u)"

usage() {
    echo "用法: $(basename "$0") install|remove|status"
    exit 1
}

[ $# -lt 1 ] && usage

case "$1" in
    install)
        mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"
        cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>$SCRIPT</string>
    <string>--port</string>
    <string>6185</string>
    <string>--interval</string>
    <string>0.5</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$LOG_DIR/monitor.out.log</string>
  <key>StandardErrorPath</key>
  <string>$LOG_DIR/monitor.err.log</string>
</dict>
</plist>
EOF
        chmod 600 "$PLIST"
        launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
        launchctl bootstrap "$DOMAIN" "$PLIST"
        launchctl kickstart -k "$DOMAIN/$LABEL"
        echo "✓ H5 来源 IP 监测已安装并启动"
        ;;
    remove)
        launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
        rm -f "$PLIST"
        echo "✓ H5 来源 IP 监测已移除"
        ;;
    status)
        if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
            launchctl print "$DOMAIN/$LABEL" | head -30
        else
            echo "(未运行)"
            exit 1
        fi
        ;;
    *)
        usage
        ;;
esac
