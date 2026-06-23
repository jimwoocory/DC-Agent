#!/usr/bin/env python3
"""Testable rule engine for the DC-Agent watchdog cron entrypoint."""

from __future__ import annotations

import argparse
import dataclasses
import re
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

DC_ROOT = Path("/Users/dianchi/DC-Agent")


@dataclasses.dataclass(frozen=True)
class ProbeSpec:
    name: str
    kind: str
    target: str
    groups: tuple[str, ...] = ("watchdog",)

    def shell_entry(self) -> str:
        return f"{self.name}|{self.kind}|{self.target}"


@dataclasses.dataclass(frozen=True)
class DisabledProbe:
    name: str
    reason: str
    groups: tuple[str, ...] = ("watchdog",)

    def shell_entry(self) -> str:
        return f"{self.name}|{self.reason}"


ACTIVE_PROBES: tuple[ProbeSpec, ...] = (
    ProbeSpec("astrbot_dashboard", "tcp", "6185", ("watchdog", "astrbot")),
    ProbeSpec("hermes_gateway", "tcp", "8644", ("watchdog", "hermes")),
    ProbeSpec("astrbot_response", "tcp", "8645", ("watchdog", "astrbot")),
    ProbeSpec("hermes_webui_thirdparty", "tcp", "8787", ("watchdog", "hermes")),
    ProbeSpec("hermes_webui", "tcp", "9119", ("watchdog", "hermes")),
    ProbeSpec("openclaw_watchdog", "tcp", "9120", ("watchdog", "openclaw")),
    ProbeSpec(
        "astrbot_api",
        "http",
        "http://127.0.0.1:6185/api/stat/start-time",
        ("watchdog", "astrbot"),
    ),
    ProbeSpec(
        "assistant_chat_health",
        "http_strict",
        "http://127.0.0.1:6185/api/chat/health",
        ("watchdog", "astrbot", "assistant"),
    ),
    ProbeSpec(
        "openclaw_watchdog_status",
        "http",
        "http://127.0.0.1:9120/status",
        ("watchdog", "openclaw"),
    ),
    ProbeSpec(
        "system_entries_plugin",
        "http",
        "http://127.0.0.1:6185/api/plug/system_entries/health",
        ("watchdog", "astrbot"),
    ),
    ProbeSpec(
        "dashboard_quick_entries",
        "dashboard_quick_entries",
        "data/dist/index.html",
        ("watchdog", "dashboard"),
    ),
    ProbeSpec(
        "knowledge_cycle", "knowledge_cycle", "cron_tick", ("watchdog", "knowledge")
    ),
)


DISABLED_PROBES: tuple[DisabledProbe, ...] = (
    DisabledProbe(
        "nas_watchdog_heartbeat",
        "NAS/Feishu sync jobs paused by operator request on 2026-06-04",
        ("nas", "sync", "watchdog"),
    ),
    DisabledProbe(
        "feishu_sync_heartbeat",
        "NAS/Feishu sync jobs paused by operator request on 2026-06-04",
        ("nas", "sync", "watchdog"),
    ),
)


MAINTENANCE_SUPPRESSED_SERVICES: frozenset[str] = frozenset(
    {
        "astrbot_dashboard",
        "astrbot_response",
        "astrbot_api",
        "assistant_chat_health",
        "system_entries_plugin",
        "dashboard_quick_entries",
        "hermes_gateway",
        "hermes_webui",
        "hermes_webui_thirdparty",
        "openclaw_watchdog",
        "openclaw_watchdog_status",
    }
)


def active_probe_names() -> set[str]:
    return {probe.name for probe in ACTIVE_PROBES}


def disabled_probe_names() -> set[str]:
    return {probe.name for probe in DISABLED_PROBES}


def probe_enabled(name: str) -> bool:
    return name in active_probe_names()


def dashboard_static_assets_ready(dist_path: Path | str) -> bool:
    dist = Path(dist_path)
    return dist.is_dir() and (dist / "index.html").is_file()


def should_suppress_for_agent_maintenance(service: str) -> bool:
    return service in MAINTENANCE_SUPPRESSED_SERVICES


def elapsed_seconds(etime: str) -> int:
    days = 0
    rest = etime.strip()
    if "-" in rest:
        day_part, rest = rest.split("-", 1)
        days = int(day_part or 0)
    parts = [int(part) for part in rest.split(":")]
    if len(parts) == 3:
        hours, minutes, seconds = parts
    elif len(parts) == 2:
        hours = 0
        minutes, seconds = parts
    else:
        return days * 86400
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def agent_maintenance_reason_from_rows(
    rows: Iterable[str],
    *,
    grace_seconds: int,
    dc_root: Path | str,
) -> str | None:
    root_text = str(dc_root)
    matches: list[str] = []
    for raw in rows:
        row = raw.strip()
        match = re.match(r"(\d+)\s+(\S+)\s+(.+)", row)
        if not match:
            continue
        pid, etime, command = match.groups()
        if "dc-watchdog.sh" in command or "diagnose.sh" in command:
            continue
        if "/data/watchdog/incidents/" in command:
            continue
        is_recent = elapsed_seconds(etime) <= grace_seconds
        is_workspace_agent = root_text in command
        if not (is_recent or is_workspace_agent):
            continue
        if re.search(r"(^|[/\s])(codex|claude)(\s|$)", command):
            matches.append(f"{pid}:{command[:160]}")
    if not matches:
        return None
    return "agent_maintenance_cli_active:" + " | ".join(matches[:3])


def current_agent_maintenance_reason(
    *,
    grace_seconds: int,
    dc_root: Path | str,
) -> str | None:
    try:
        rows = subprocess.check_output(
            ["ps", "-Ao", "pid=,etime=,command="],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=3,
        ).splitlines()
    except Exception:
        return None
    return agent_maintenance_reason_from_rows(
        rows,
        grace_seconds=grace_seconds,
        dc_root=dc_root,
    )


def _print_entries(entries: Iterable[ProbeSpec | DisabledProbe]) -> None:
    for entry in entries:
        print(entry.shell_entry())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DC-Agent watchdog rule engine")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list-active")
    subparsers.add_parser("list-disabled")
    maintenance = subparsers.add_parser("maintenance-reason")
    maintenance.add_argument("--grace-sec", type=int, required=True)
    maintenance.add_argument("--dc-root", default=str(DC_ROOT))
    args = parser.parse_args(argv)

    if args.command == "list-active":
        _print_entries(ACTIVE_PROBES)
        return 0
    if args.command == "list-disabled":
        _print_entries(DISABLED_PROBES)
        return 0
    if args.command == "maintenance-reason":
        reason = current_agent_maintenance_reason(
            grace_seconds=args.grace_sec,
            dc_root=args.dc_root,
        )
        if not reason:
            return 1
        print(reason)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
