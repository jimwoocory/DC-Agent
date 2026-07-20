#!/bin/bash
# DC-Agent 启动脚本（重装后简化版）
# 仅启动 AstrBot 主服务 — Hermes 由独立 launchd 管，dashboard 用 AstrBot 内置 WebUI（不再起 Vue dev 服务）。

export PATH="/opt/homebrew/bin:/Users/dianchi/.local/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
cd /Users/dianchi/DC-Agent

# Load local secrets for launchd-started services. This file is outside the repo.
if [ -f "$HOME/.dc-agent.env" ]; then
  set -a
  . "$HOME/.dc-agent.env"
  set +a
fi

# Use the NAS LAN origin until the domestic workbench domain is online. The
# same variable switches all entrypoints to HTTPS without changing H5 routes.
# DC_ASSISTANT_H5_ADMIN_TOKEN must match the NAS deployment so capabilities are
# created in the same process that serves the H5 workspace. Prefer Keychain so
# the shared token does not need to live in a plaintext environment file.
export DC_ASSISTANT_H5_ORIGIN="${DC_ASSISTANT_H5_ORIGIN:-http://192.168.1.35:6185}"
case "$DC_ASSISTANT_H5_ORIGIN" in
  https://*) export DC_ASSISTANT_H5_ORIGIN ;;
  http://192.168.1.35:6185) export DC_ASSISTANT_H5_ORIGIN ;;
  *)
    echo "DC_ASSISTANT_H5_ORIGIN must be the NAS LAN origin or an HTTPS origin" >&2
    exit 1
    ;;
esac
if [ -z "${DC_ASSISTANT_H5_ADMIN_TOKEN:-}" ] && command -v security >/dev/null 2>&1; then
  DC_ASSISTANT_H5_ADMIN_TOKEN="$(
    security find-generic-password \
      -a dc-agent \
      -s dc-agent-assistant-h5-admin-token \
      -w 2>/dev/null || true
  )"
  export DC_ASSISTANT_H5_ADMIN_TOKEN
fi
export DC_AI_CDR_QUEUE_ROOT="${DC_AI_CDR_QUEUE_ROOT:-$HOME/nas_kb/AI转CDR工具/AI转CDR共享}"

# Keep the desktop binding secret out of plaintext files. The Vercel project
# must use the same value so its 60-second assertions verify on this service.
if [ -z "${DESKTOP_BINDING_SECRET:-}" ] && command -v security >/dev/null 2>&1; then
  DESKTOP_BINDING_SECRET="$(
    security find-generic-password \
      -a dc-agent \
      -s com.dianchi.desktop.binding \
      -w 2>/dev/null || true
  )"
  export DESKTOP_BINDING_SECRET
fi

exec .venv/bin/python main.py
