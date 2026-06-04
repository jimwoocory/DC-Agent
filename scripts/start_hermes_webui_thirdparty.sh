#!/usr/bin/env bash
set -euo pipefail

WEBUI_ROOT="/Users/dianchi/DC-Agent/hermes-webui"
AGENT_ROOT="/Users/dianchi/DC-Agent/hermes-agent"

export HERMES_HOME="/Users/dianchi/DC-Agent/hermes-config"
export HERMES_WEBUI_STATE_DIR="/Users/dianchi/DC-Agent/hermes-webui-state"
export HERMES_WEBUI_AGENT_DIR="$AGENT_ROOT"
export HERMES_WEBUI_PYTHON="$AGENT_ROOT/.venv/bin/python"
export PYTHONPATH="$AGENT_ROOT"
export HERMES_WEBUI_HOST="127.0.0.1"
export HERMES_WEBUI_PORT="8787"

cd "$WEBUI_ROOT"
exec "$HERMES_WEBUI_PYTHON" server.py
