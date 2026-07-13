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
