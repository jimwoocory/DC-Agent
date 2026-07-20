#!/bin/sh
set -eu

if [ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ]; then
  exec dbus-run-session -- "$0" "$@"
fi

mkdir -p /root/.local/share/keyrings "${XDG_RUNTIME_DIR:-/tmp/xdg-runtime}"
chmod 700 /root/.local/share/keyrings "${XDG_RUNTIME_DIR:-/tmp/xdg-runtime}"

if command -v gnome-keyring-daemon >/dev/null 2>&1; then
  keyring_environment="$(printf '\n' | gnome-keyring-daemon --unlock --components=secrets)"
  eval "$keyring_environment"
fi

exec "$@"
