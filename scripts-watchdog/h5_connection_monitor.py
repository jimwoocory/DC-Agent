#!/usr/bin/env python3
"""Record privacy-safe TCP connection metadata for the assistant H5 service."""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

DC_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HISTORY_PATH = (
    DC_ROOT / "data" / "watchdog" / "h5_connections" / "history.jsonl"
)
LSOF_CONNECTION = re.compile(
    r"TCP\s+.+:(?P<local_port>\d+)->(?P<source_ip>.+):"
    r"(?P<source_port>\d+)\s+\((?P<state>[^)]+)\)"
)


def collect_connections(port: int) -> list[dict[str, str | int]]:
    """Collect current non-loopback TCP peers connected to one local port.

    Args:
        port: Local TCP port exposed by the H5 service.

    Returns:
        Privacy-safe connection records without URLs or application payloads.
    """
    observed_at = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    records: list[dict[str, str | int]] = []
    result = subprocess.run(
        ["/usr/sbin/lsof", "-nP", f"-iTCP:{port}"],
        capture_output=True,
        check=False,
        text=True,
        timeout=5,
    )
    for line in result.stdout.splitlines():
        match = LSOF_CONNECTION.search(line)
        if match is None or int(match.group("local_port")) != port:
            continue
        try:
            source_ip = ipaddress.ip_address(match.group("source_ip").strip("[]"))
        except ValueError:
            continue
        if source_ip.is_loopback:
            continue
        records.append(
            {
                "observed_at": observed_at,
                "source_ip": str(source_ip),
                "source_port": int(match.group("source_port")),
                "local_port": port,
                "tcp_state": match.group("state"),
            }
        )
    return records


def run_monitor(port: int, interval: float, history_path: Path, once: bool) -> int:
    """Poll TCP metadata and append each new connection state once.

    Args:
        port: Local TCP port to inspect.
        interval: Seconds between connection-table polls.
        history_path: JSONL file receiving privacy-safe records.
        once: Exit after one poll when true.

    Returns:
        Zero after a successful one-shot run or normal shutdown.
    """
    history_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.touch(mode=0o600, exist_ok=True)
    history_path.chmod(0o600)
    previous: set[tuple[str, int, str]] = set()

    while True:
        records = collect_connections(port)
        active: set[tuple[str, int, str]] = set()
        with history_path.open("a", encoding="utf-8") as history:
            for record in records:
                key = (
                    str(record["source_ip"]),
                    int(record["source_port"]),
                    str(record["tcp_state"]),
                )
                active.add(key)
                if key in previous:
                    continue
                history.write(json.dumps(record, ensure_ascii=False) + "\n")
        previous = active
        if once:
            return 0
        time.sleep(max(0.2, interval))


def main() -> int:
    """Run the H5 TCP connection monitor from command-line arguments.

    Returns:
        Process exit status.
    """
    parser = argparse.ArgumentParser(
        description="Record source IP and TCP state for the assistant H5 port"
    )
    parser.add_argument("--port", type=int, default=6185)
    parser.add_argument("--interval", type=float, default=0.5)
    parser.add_argument("--history-path", type=Path, default=DEFAULT_HISTORY_PATH)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    return run_monitor(args.port, args.interval, args.history_path, args.once)


if __name__ == "__main__":
    raise SystemExit(main())
