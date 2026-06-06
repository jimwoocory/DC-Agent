#!/usr/bin/env python3
"""Unified control surface for DC-Agent scheduled jobs and watchdogs.

This script intentionally uses only Python's standard library.  It manages the
places where DC-Agent background work currently lives:

* launchd user agents
* marker-managed crontab blocks
* Codex heartbeat automation TOML files
* known dc-watchdog probe names
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import plistlib
import re
import subprocess
import sys
from pathlib import Path

DC_ROOT = Path(os.environ.get("DC_AGENT_ROOT", Path(__file__).resolve().parents[1]))
HOME = Path(os.environ.get("HOME", str(Path.home())))
UID = os.getuid()


@dataclasses.dataclass(frozen=True)
class LaunchdJob:
    key: str
    label: str
    plist: Path
    groups: tuple[str, ...]
    description: str


@dataclasses.dataclass(frozen=True)
class CronJob:
    key: str
    marker_regex: str
    line_regex: str
    install_script: Path | None
    groups: tuple[str, ...]
    description: str


@dataclasses.dataclass(frozen=True)
class CodexAutomation:
    key: str
    toml: Path
    groups: tuple[str, ...]
    description: str


LAUNCHD_JOBS: tuple[LaunchdJob, ...] = (
    LaunchdJob(
        key="dianchi-tech-night",
        label="io.dianchi.tech.night",
        plist=HOME / "Library/LaunchAgents/io.dianchi.tech.night.plist",
        groups=("night", "dianchi-tech", "nas"),
        description="01:00 巅池-技术日报 night 生成任务",
    ),
    LaunchdJob(
        key="dianchi-tech-report",
        label="io.dianchi.tech.report",
        plist=HOME / "Library/LaunchAgents/io.dianchi.tech.report.plist",
        groups=("night", "dianchi-tech", "nas"),
        description="09:00 巅池-技术日报 report 推送任务",
    ),
    LaunchdJob(
        key="baidu-nas-sync",
        label="com.dcagent.baidu-nas-sync",
        plist=HOME / "Library/LaunchAgents/com.dcagent.baidu-nas-sync.plist",
        groups=("night", "nas", "sync"),
        description="02:00 百度网盘到 NAS 同步",
    ),
    LaunchdJob(
        key="feishu-sync",
        label="com.dcagent.feishu-sync",
        plist=HOME / "Library/LaunchAgents/com.dcagent.feishu-sync.plist",
        groups=("nas", "sync"),
        description="飞书云盘到 NAS 同步",
    ),
    LaunchdJob(
        key="nas-watchdog",
        label="com.dcagent.nas-watchdog",
        plist=HOME / "Library/LaunchAgents/com.dcagent.nas-watchdog.plist",
        groups=("nas", "sync", "watchdog"),
        description="NAS sync 老 watchdog heartbeat",
    ),
)


CRON_JOBS: tuple[CronJob, ...] = (
    CronJob(
        key="dc-watchdog",
        marker_regex=r"DC-Agent watchdog",
        line_regex=r"scripts-watchdog/dc-watchdog\.sh",
        install_script=DC_ROOT / "scripts-watchdog/install-cron.sh",
        groups=("watchdog", "nas"),
        description="每分钟 DC-Agent 总探活和告警",
    ),
    CronJob(
        key="dianchi-tech-cron",
        marker_regex=r"巅池-技术 日报",
        line_regex=r"dianchi-tech-(night|report)\.sh",
        install_script=DC_ROOT / "scripts-tools/install-dianchi-tech-cron.sh",
        groups=("night", "dianchi-tech", "nas"),
        description="旧版 crontab 巅池-技术日报入口",
    ),
    CronJob(
        key="onboarding-watch",
        marker_regex=r"问卷→入职卡 轮询",
        line_regex=r"check_and_push_onboarding\.py",
        install_script=DC_ROOT / "scripts-tools/install-onboarding-watch-cron.sh",
        groups=("onboarding",),
        description="问卷填表到入职卡推送轮询",
    ),
)


CODEX_AUTOMATIONS: tuple[CodexAutomation, ...] = (
    CodexAutomation(
        key="nas",
        toml=HOME / ".codex/automations/nas/automation.toml",
        groups=("nas", "night"),
        description="飞书 NAS 学习夜间测试复盘 heartbeat",
    ),
    CodexAutomation(
        key="nas-workflow",
        toml=HOME / ".codex/automations/nas-workflow/automation.toml",
        groups=("nas", "sync"),
        description="飞书云文档到 NAS knowledge workflow heartbeat",
    ),
)


WATCHDOG_PROBES: dict[str, tuple[str, ...]] = {
    "nas_watchdog_heartbeat": ("nas", "sync", "watchdog"),
    "feishu_sync_heartbeat": ("nas", "sync", "watchdog"),
}


def run(args: list[str], *, check: bool = False) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(args, text=True, capture_output=True, check=check)
    except PermissionError as exc:
        return subprocess.CompletedProcess(args, 126, "", str(exc))


def crontab_text() -> str:
    proc = run(["crontab", "-l"])
    if proc.returncode == 126:
        raise RuntimeError(f"crontab inaccessible: {proc.stderr}")
    return proc.stdout if proc.returncode == 0 else ""


def install_crontab(text: str) -> None:
    proc = subprocess.run(["crontab", "-"], input=text, text=True, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "crontab install failed")


def remove_cron_job(job: CronJob) -> bool:
    text = crontab_text()
    if not text:
        return False
    out: list[str] = []
    removed = False
    skip_next = False
    marker = re.compile(job.marker_regex)
    line = re.compile(job.line_regex)
    for raw in text.splitlines():
        if marker.search(raw):
            removed = True
            skip_next = True
            continue
        if line.search(raw):
            removed = True
            skip_next = False
            continue
        if skip_next:
            removed = True
            skip_next = False
            continue
        out.append(raw)
    if removed:
        install_crontab("\n".join(out).rstrip() + "\n")
    return removed


def cron_installed(job: CronJob) -> bool:
    text = crontab_text()
    return bool(re.search(job.marker_regex, text) or re.search(job.line_regex, text))


def launchd_loaded(label: str) -> bool:
    proc = run(["launchctl", "list"])
    return proc.returncode == 0 and any(
        line.rstrip().endswith(label) for line in proc.stdout.splitlines()
    )


def launchd_disabled(label: str) -> bool | None:
    proc = run(["launchctl", "print-disabled", f"gui/{UID}"])
    if proc.returncode != 0:
        return None
    match = re.search(rf'"{re.escape(label)}"\s*=>\s*(enabled|disabled)', proc.stdout)
    if not match:
        return None
    return match.group(1) == "disabled"


def launchd_plist_summary(path: Path) -> str:
    if not path.exists():
        return "missing plist"
    try:
        data = plistlib.loads(path.read_bytes())
    except Exception as exc:  # noqa: BLE001
        return f"plist unreadable: {exc}"
    if "StartCalendarInterval" in data:
        return f"calendar={data['StartCalendarInterval']}"
    if "StartInterval" in data:
        return f"interval={data['StartInterval']}s"
    if data.get("RunAtLoad"):
        return "RunAtLoad"
    return "manual/keepalive"


def pause_launchd(job: LaunchdJob) -> None:
    run(["launchctl", "disable", f"gui/{UID}/{job.label}"])
    if job.plist.exists():
        run(["launchctl", "bootout", f"gui/{UID}", str(job.plist)])


def resume_launchd(job: LaunchdJob) -> None:
    run(["launchctl", "enable", f"gui/{UID}/{job.label}"])
    if job.plist.exists():
        run(["launchctl", "bootstrap", f"gui/{UID}", str(job.plist)])


def codex_status(job: CodexAutomation) -> str:
    if not job.toml.exists():
        return "missing"
    text = job.toml.read_text()
    match = re.search(r'^status\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return match.group(1) if match else "unknown"


def set_codex_status(job: CodexAutomation, status: str) -> bool:
    if not job.toml.exists():
        return False
    text = job.toml.read_text()
    new, count = re.subn(
        r'^status\s*=\s*"[^"]+"',
        f'status = "{status}"',
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if count == 0:
        return False
    job.toml.write_text(new)
    return True


def probe_enabled(name: str) -> bool:
    script = DC_ROOT / "scripts-watchdog/dc-watchdog.sh"
    if not script.exists():
        return False
    text = script.read_text(errors="ignore")
    active_services = re.search(r"SERVICES=\((.*?)\n\)", text, flags=re.DOTALL)
    if not active_services:
        return False
    for line in active_services.group(1).splitlines():
        stripped = line.strip()
        if not stripped.startswith('"'):
            continue
        match = re.match(r'"([^"\n]+)"', stripped)
        if not match:
            continue
        entry = match.group(1)
        service_name = entry.split("|", 1)[0]
        if service_name == name:
            return True
    return False


def in_group(groups: tuple[str, ...], selected: str) -> bool:
    return selected == "all" or selected in groups


def selected_launchd(group: str) -> list[LaunchdJob]:
    return [job for job in LAUNCHD_JOBS if in_group(job.groups, group)]


def selected_cron(group: str) -> list[CronJob]:
    return [job for job in CRON_JOBS if in_group(job.groups, group)]


def selected_codex(group: str) -> list[CodexAutomation]:
    return [job for job in CODEX_AUTOMATIONS if in_group(job.groups, group)]


def find_launchd(key: str) -> LaunchdJob:
    for job in LAUNCHD_JOBS:
        if job.key == key:
            return job
    raise KeyError(f"unknown launchd job: {key}")


def find_cron(key: str) -> CronJob:
    for job in CRON_JOBS:
        if job.key == key:
            return job
    raise KeyError(f"unknown cron job: {key}")


def find_codex(key: str) -> CodexAutomation:
    for job in CODEX_AUTOMATIONS:
        if job.key == key:
            return job
    raise KeyError(f"unknown codex automation: {key}")


def print_status(group: str) -> None:
    status = collect_status(group)
    print(f"== DC-Agent watchdog status: {group} ==")
    print("\n[launchd]")
    for item in status["launchd"]:
        print(
            f"- {item['key']}: {item['enabled_state']}, "
            f"{item['loaded_state']}, {item['schedule']}"
        )
    print("\n[cron]")
    for item in status["cron"]:
        print(f"- {item['key']}: {item['state']}")
    print("\n[codex]")
    for item in status["codex"]:
        print(f"- {item['key']}: {item['status']}")
    print("\n[dc-watchdog probes]")
    for item in status["probes"]:
        print(f"- {item['key']}: {item['state']}")


def collect_status(group: str) -> dict:
    launchd = []
    for job in selected_launchd(group):
        disabled = launchd_disabled(job.label)
        enabled_state = (
            "unknown" if disabled is None else ("disabled" if disabled else "enabled")
        )
        launchd.append(
            {
                "key": job.key,
                "label": job.label,
                "description": job.description,
                "groups": list(job.groups),
                "enabled_state": enabled_state,
                "loaded_state": "loaded" if launchd_loaded(job.label) else "not-loaded",
                "schedule": launchd_plist_summary(job.plist),
                "plist": str(job.plist),
            }
        )

    cron = []
    for job in selected_cron(group):
        try:
            state = "installed" if cron_installed(job) else "not-installed"
        except RuntimeError:
            state = "inaccessible"
        cron.append(
            {
                "key": job.key,
                "description": job.description,
                "groups": list(job.groups),
                "state": state,
            }
        )

    codex = [
        {
            "key": job.key,
            "description": job.description,
            "groups": list(job.groups),
            "status": codex_status(job),
            "toml": str(job.toml),
        }
        for job in selected_codex(group)
    ]

    probes = [
        {
            "key": name,
            "groups": list(groups),
            "state": "enabled" if probe_enabled(name) else "disabled",
        }
        for name, groups in WATCHDOG_PROBES.items()
        if in_group(groups, group)
    ]

    return {
        "group": group,
        "launchd": launchd,
        "cron": cron,
        "codex": codex,
        "probes": probes,
    }


def pause(group: str) -> None:
    for job in selected_launchd(group):
        pause_launchd(job)
    for job in selected_cron(group):
        try:
            remove_cron_job(job)
        except RuntimeError as exc:
            print(f"warning: {job.key}: {exc}", file=sys.stderr)
    for job in selected_codex(group):
        set_codex_status(job, "PAUSED")
    print_status(group)


def resume(group: str) -> None:
    for job in selected_launchd(group):
        resume_launchd(job)
    for job in selected_cron(group):
        if job.install_script and job.install_script.exists():
            run([str(job.install_script), "install"])
    for job in selected_codex(group):
        set_codex_status(job, "ACTIVE")
    print_status(group)


def pause_one(kind: str, key: str) -> None:
    if kind == "launchd":
        pause_launchd(find_launchd(key))
    elif kind == "cron":
        remove_cron_job(find_cron(key))
    elif kind == "codex":
        set_codex_status(find_codex(key), "PAUSED")
    else:
        raise KeyError(f"unsupported item type: {kind}")


def resume_one(kind: str, key: str) -> None:
    if kind == "launchd":
        resume_launchd(find_launchd(key))
    elif kind == "cron":
        job = find_cron(key)
        if job.install_script and job.install_script.exists():
            run([str(job.install_script), "install"])
    elif kind == "codex":
        set_codex_status(find_codex(key), "ACTIVE")
    else:
        raise KeyError(f"unsupported item type: {kind}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Control DC-Agent watchdog/scheduled jobs."
    )
    parser.add_argument(
        "command", choices=("status", "pause", "resume", "pause-one", "resume-one")
    )
    parser.add_argument(
        "group",
        nargs="?",
        default="all",
        help="group for status/pause/resume, or item type for pause-one/resume-one",
    )
    parser.add_argument("key", nargs="?", help="item key for pause-one/resume-one")
    parser.add_argument(
        "--json", action="store_true", help="print machine-readable JSON"
    )
    args = parser.parse_args(argv)
    if args.command == "status":
        if args.json:
            print(json.dumps(collect_status(args.group), ensure_ascii=False))
        else:
            print_status(args.group)
    elif args.command == "pause":
        pause(args.group)
    elif args.command == "resume":
        resume(args.group)
    elif args.command == "pause-one":
        if not args.key:
            parser.error("pause-one requires item type and key")
        pause_one(args.group, args.key)
    elif args.command == "resume-one":
        if not args.key:
            parser.error("resume-one requires item type and key")
        resume_one(args.group, args.key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
