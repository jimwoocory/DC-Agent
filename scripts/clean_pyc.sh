#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="${1:-clean}"

if [[ "$MODE" != "clean" && "$MODE" != "--dry-run" ]]; then
  echo "Usage: scripts/clean_pyc.sh [--dry-run]" >&2
  exit 2
fi

cd "$ROOT_DIR"

if [[ "$MODE" == "--dry-run" ]]; then
  find . \
    \( -path "./.git" -o -name ".venv" -o -path "./.uv-cache" -o -path "./hermes-agent" -o -path "./hermes-config" -o -path "./hermes-webui" -o -path "./hermes-webui-state" \) -prune -o \
    \( -type d -name "__pycache__" -o -type d -name ".pytest_cache" -o -type d -name ".ruff_cache" -o -type f -name "*.pyc" -o -type f -name "*.pyo" \) \
    -print
  exit 0
fi

find . \
  \( -path "./.git" -o -name ".venv" -o -path "./.uv-cache" -o -path "./hermes-agent" -o -path "./hermes-config" -o -path "./hermes-webui" -o -path "./hermes-webui-state" \) -prune -o \
  \( -type d -name "__pycache__" -o -type d -name ".pytest_cache" -o -type d -name ".ruff_cache" \) \
  -prune -exec rm -rf {} +

find . \
  \( -path "./.git" -o -name ".venv" -o -path "./.uv-cache" -o -path "./hermes-agent" -o -path "./hermes-config" -o -path "./hermes-webui" -o -path "./hermes-webui-state" \) -prune -o \
  \( -type f -name "*.pyc" -o -type f -name "*.pyo" \) \
  -delete
