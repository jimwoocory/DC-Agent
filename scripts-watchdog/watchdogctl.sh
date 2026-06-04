#!/bin/bash
set -euo pipefail

exec /usr/bin/env python3 /Users/dianchi/DC-Agent/scripts-watchdog/watchdogctl.py "$@"
