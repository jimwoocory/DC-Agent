#!/bin/bash
# DC-Agent 启动脚本（重装后简化版）
# 仅启动 AstrBot 主服务 — Hermes 由独立 launchd 管，dashboard 用 AstrBot 内置 WebUI（不再起 Vue dev 服务）。

export PATH="/opt/homebrew/bin:/Users/dianchi/.local/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
cd /Users/dianchi/DC-Agent

exec .venv/bin/python main.py
