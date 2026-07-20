#!/usr/bin/env python3
"""Unified control surface for DC-Agent scheduled jobs and watchdogs.

This script intentionally uses only Python's standard library.  It manages the
places where DC-Agent background work currently lives:

* launchd user agents
* marker-managed crontab blocks
* Codex heartbeat automation TOML files
* known dc-watchdog probe names
* the read-only Task Control Plane snapshot
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import hmac
import importlib.util
import json
import os
import plistlib
import re
import subprocess
import sys
import time
from pathlib import Path

DC_ROOT = Path(os.environ.get("DC_AGENT_ROOT", Path(__file__).resolve().parents[1]))
HOME = Path(os.environ.get("HOME", str(Path.home())))
UID = os.getuid()
CONTROL_PLAN_TTL_SECONDS = 120


@dataclasses.dataclass(frozen=True)
class LaunchdJob:
    key: str
    label: str
    plist: Path
    groups: tuple[str, ...]
    description: str
    controllable: bool = True
    replacement_task_ids: tuple[str, ...] = ()
    group_pause_protected: bool = False
    impact_level: str = "standard"
    impact_summary: str = ""


@dataclasses.dataclass(frozen=True)
class CronJob:
    key: str
    marker_regex: str
    line_regex: str
    install_script: Path | None
    groups: tuple[str, ...]
    description: str
    controllable: bool = True
    replacement_task_ids: tuple[str, ...] = ()
    group_pause_protected: bool = False
    impact_level: str = "standard"
    impact_summary: str = ""


@dataclasses.dataclass(frozen=True)
class CodexAutomation:
    key: str
    toml: Path
    groups: tuple[str, ...]
    description: str
    impact_level: str = "standard"
    impact_summary: str = ""


LAUNCHD_JOBS: tuple[LaunchdJob, ...] = (
    LaunchdJob(
        key="astrbot-runtime",
        label="io.astrbot.bot",
        plist=HOME / "Library/LaunchAgents/io.astrbot.bot.plist",
        groups=("core", "astrbot", "watchdog"),
        description="AstrBot 主运行时",
        controllable=False,
    ),
    LaunchdJob(
        key="hermes-gateway",
        label="ai.hermes.gateway",
        plist=HOME / "Library/LaunchAgents/ai.hermes.gateway.plist",
        groups=("core", "hermes", "watchdog"),
        description="Hermes gateway",
        controllable=False,
    ),
    LaunchdJob(
        key="hermes-dashboard",
        label="ai.hermes.dashboard",
        plist=HOME / "Library/LaunchAgents/ai.hermes.dashboard.plist",
        groups=("core", "hermes", "watchdog"),
        description="Hermes dashboard",
        controllable=False,
    ),
    LaunchdJob(
        key="hermes-webui-thirdparty",
        label="ai.hermes.webui.thirdparty",
        plist=HOME / "Library/LaunchAgents/ai.hermes.webui.thirdparty.plist",
        groups=("core", "hermes", "watchdog"),
        description="Hermes third-party WebUI",
        controllable=False,
    ),
    LaunchdJob(
        key="gmail-promo-cleaner",
        label="com.dcagent.gmail-promo-cleaner",
        plist=HOME / "Library/LaunchAgents/com.dcagent.gmail-promo-cleaner.plist",
        groups=("automation", "email"),
        description="Gmail 推广邮件清理",
        controllable=False,
    ),
    LaunchdJob(
        key="cmd-config-watchdog",
        label="io.dcagent.cmd-config-watchdog",
        plist=HOME / "Library/LaunchAgents/io.dcagent.cmd-config-watchdog.plist",
        groups=("core", "security", "watchdog"),
        description="AstrBot 配置防脱敏监控",
        controllable=False,
    ),
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
        controllable=False,
        replacement_task_ids=("knowledge_cycle:feishu_nas_workflow",),
    ),
    LaunchdJob(
        key="nas-watchdog",
        label="com.dcagent.nas-watchdog",
        plist=HOME / "Library/LaunchAgents/com.dcagent.nas-watchdog.plist",
        groups=("nas", "sync", "watchdog"),
        description="NAS sync 老 watchdog heartbeat",
        controllable=False,
        replacement_task_ids=("knowledge_cycle:mount",),
    ),
)


CRON_JOBS: tuple[CronJob, ...] = (
    CronJob(
        key="astrbot-http-watchdog",
        marker_regex=r"^# DC-Agent AstrBot HTTP watchdog$",
        line_regex=r"\.local/bin/astrbot_watchdog\.sh",
        install_script=None,
        groups=("core", "astrbot", "watchdog"),
        description="旧版 AstrBot HTTP 自动拉起检查",
        controllable=False,
        replacement_task_ids=(
            "launchd:astrbot-runtime",
            "watchdog_probe:astrbot_api",
        ),
    ),
    CronJob(
        key="employee-usage-audit",
        marker_regex=r"DC-Agent employee usage audit",
        line_regex=r"employee_usage_audit\.py",
        install_script=DC_ROOT / "scripts-tools/install-employee-usage-audit-cron.sh",
        groups=("assistant", "analytics"),
        description="员工使用情况审计",
        controllable=False,
    ),
    CronJob(
        key="dc-watchdog",
        marker_regex=r"DC-Agent watchdog",
        line_regex=r"scripts-watchdog/dc-watchdog\.sh",
        install_script=DC_ROOT / "scripts-watchdog/install-cron.sh",
        groups=("watchdog", "nas"),
        description="每分钟 DC-Agent 总探活和告警",
        group_pause_protected=True,
        impact_level="critical",
        impact_summary="将停止统一探活、告警和 Knowledge Cycle 每分钟调度",
    ),
    CronJob(
        key="dianchi-tech-cron",
        marker_regex=r"巅池-技术 日报",
        line_regex=r"dianchi-tech-(night|report)\.sh",
        install_script=DC_ROOT / "scripts-tools/install-dianchi-tech-cron.sh",
        groups=("night", "dianchi-tech", "nas"),
        description="旧版 crontab 巅池-技术日报入口",
        controllable=False,
        replacement_task_ids=(
            "launchd:dianchi-tech-night",
            "launchd:dianchi-tech-report",
        ),
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


def _load_watchdog_engine():
    module_path = DC_ROOT / "scripts-watchdog" / "watchdog_engine.py"
    spec = importlib.util.spec_from_file_location("watchdog_engine", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load watchdog engine: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_repair_engine():
    """Load the deterministic watchdog repair engine.

    Returns:
        Loaded repair engine Python module.

    Raises:
        RuntimeError: If the module cannot be loaded from the configured root.
    """
    module_path = DC_ROOT / "scripts-watchdog" / "repair_engine.py"
    spec = importlib.util.spec_from_file_location("repair_engine", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load repair engine: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_task_control_plane():
    """Load the Task Control Plane module from the operational scripts path.

    Returns:
        Loaded Task Control Plane Python module.

    Raises:
        RuntimeError: If the module cannot be loaded from the configured root.
    """
    module_path = DC_ROOT / "scripts-watchdog" / "task_control_plane.py"
    spec = importlib.util.spec_from_file_location("task_control_plane", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load task control plane: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def watchdog_probe_groups() -> dict[str, tuple[str, ...]]:
    engine = _load_watchdog_engine()
    return {
        probe.name: probe.groups
        for probe in (*engine.ACTIVE_PROBES, *engine.DISABLED_PROBES)
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
    proc = run(["launchctl", "print", f"gui/{UID}/{label}"])
    return proc.returncode == 0


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
    return bool(_load_watchdog_engine().probe_enabled(name))


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
    repair = status["repair"]
    print("\n[watchdog repair]")
    print(
        f"- state={repair['state_status']}, services={repair['service_count']}, "
        f"recent_results={repair['recent_result_count']}, "
        f"pending_reviews={repair['pending_review_count']}"
    )
    for item in repair["services"]:
        print(
            f"- {item['service']}: {item['state']}, "
            f"attempts_remaining={item['attempts_remaining']}, "
            f"circuit_remaining_seconds={item['circuit_remaining_seconds']}"
        )
    analytics_summary = repair.get("analytics", {}).get("dashboard_summary", {})
    if analytics_summary:
        print(
            "- reliability="
            f"{analytics_summary['status']}, "
            f"auto_attempts_24h={analytics_summary['auto_attempts_24h']}, "
            f"success_rate_24h={analytics_summary['success_rate_24h']}, "
            f"open_circuits={analytics_summary['open_circuits']}, "
            f"retention_candidates={analytics_summary['retention_candidates']}, "
            "codex_review_recommended="
            f"{analytics_summary['codex_review_recommended']}"
        )
    if repair["source_errors"]:
        print(f"- source_errors={repair['source_errors']}")
    control_plane = status["control_plane"]
    print("\n[task control plane]")
    print(
        f"- mode={control_plane['mode']}, tasks={control_plane['task_count']}, "
        f"statuses={control_plane['status_counts']}"
    )
    if control_plane["source_errors"]:
        print(f"- source_errors={control_plane['source_errors']}")


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
                "controllable": job.controllable,
                "replacement_task_ids": list(job.replacement_task_ids),
                "group_pause_protected": job.group_pause_protected,
                "impact_level": job.impact_level,
                "impact_summary": job.impact_summary,
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
                "controllable": job.controllable,
                "replacement_task_ids": list(job.replacement_task_ids),
                "group_pause_protected": job.group_pause_protected,
                "impact_level": job.impact_level,
                "impact_summary": job.impact_summary,
            }
        )

    codex = [
        {
            "key": job.key,
            "description": job.description,
            "groups": list(job.groups),
            "status": codex_status(job),
            "toml": str(job.toml),
            "impact_level": job.impact_level,
            "impact_summary": job.impact_summary,
        }
        for job in selected_codex(group)
    ]

    probes = [
        {
            "key": name,
            "groups": list(groups),
            "state": "enabled" if probe_enabled(name) else "disabled",
        }
        for name, groups in watchdog_probe_groups().items()
        if in_group(groups, group)
    ]

    repair = _load_repair_engine().collect_repair_status(
        state_path=DC_ROOT / "data" / "watchdog" / "repair_state.json",
        events_path=DC_ROOT / "data" / "watchdog" / "repairs.jsonl",
        analytics_path=DC_ROOT / "data" / "watchdog" / "repair_analytics.json",
    )
    if group != "all":
        repair["services"] = [
            item for item in repair["services"] if group in item.get("groups", [])
        ]
        repair["recent_results"] = [
            item for item in repair["recent_results"] if group in item.get("groups", [])
        ]
        repair["reviews"] = [
            item for item in repair["reviews"] if group in item.get("groups", [])
        ]
        repair["service_count"] = len(repair["services"])
        repair["recent_result_count"] = len(repair["recent_results"])
        repair["review_count"] = len(repair["reviews"])
        repair["pending_review_count"] = sum(
            item["status"] != "resolved" for item in repair["reviews"]
        )

    status = {
        "group": group,
        "launchd": launchd,
        "cron": cron,
        "codex": codex,
        "probes": probes,
        "repair": repair,
    }
    status["control_plane"] = _load_task_control_plane().collect_control_plane(
        status,
        dc_root=DC_ROOT,
        group=group,
    )
    return status


def build_control_plan(
    group: str,
    operation: str,
    *,
    issued_at: int | None = None,
    target_kind: str | None = None,
    target_key: str | None = None,
) -> dict:
    """Build a short-lived pause or resume Control Plan.

    Args:
        group: Registered operational group.
        operation: Planned operation, either ``pause`` or ``resume``.
        issued_at: Optional issue timestamp reused while validating a plan.
        target_kind: Optional scheduler kind for an item-scoped plan.
        target_key: Optional registered scheduler key for an item-scoped plan.

    Returns:
        JSON-safe plan containing exact actions, skipped tasks, and plan ID.

    Raises:
        ValueError: If the operation or target scope is unsupported.
    """
    if operation not in {"pause", "resume"}:
        raise ValueError(f"unsupported Control Plan operation: {operation}")
    if bool(target_kind) != bool(target_key):
        raise ValueError("Control Plan target kind and key must be provided together")
    if target_kind and target_kind not in {"launchd", "cron", "codex"}:
        raise ValueError(f"unsupported Control Plan target kind: {target_kind}")
    scope = "item" if target_kind else "group"
    launchd_jobs = (
        [find_launchd(target_key)]
        if target_kind == "launchd" and target_key
        else []
        if target_kind
        else selected_launchd(group)
    )
    cron_jobs = (
        [find_cron(target_key)]
        if target_kind == "cron" and target_key
        else []
        if target_kind
        else selected_cron(group)
    )
    codex_jobs = (
        [find_codex(target_key)]
        if target_kind == "codex" and target_key
        else []
        if target_kind
        else selected_codex(group)
    )
    actions: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    for job in launchd_jobs:
        item = {
            "task_id": f"launchd:{job.key}",
            "kind": "launchd",
            "key": job.key,
            "description": job.description,
            "schedule": launchd_plist_summary(job.plist),
            "execution_identity": f"{job.label}|{job.plist}",
            "impact_level": job.impact_level,
            "impact_summary": job.impact_summary,
        }
        if job.replacement_task_ids:
            skipped.append({**item, "reason": "superseded"})
        elif not job.controllable:
            skipped.append({**item, "reason": "read_only"})
        else:
            active = (
                launchd_loaded(job.label) and launchd_disabled(job.label) is not True
            )
            if operation == "pause" and job.group_pause_protected and scope == "group":
                skipped.append({**item, "reason": "protected_controller"})
            elif operation == "pause" and not active:
                skipped.append({**item, "reason": "already_inactive"})
            elif operation == "resume" and active:
                skipped.append({**item, "reason": "already_active"})
            else:
                actions.append(item)

    for job in cron_jobs:
        item = {
            "task_id": f"crontab:{job.key}",
            "kind": "cron",
            "key": job.key,
            "description": job.description,
            "schedule": "crontab",
            "execution_identity": str(job.install_script or ""),
            "impact_level": job.impact_level,
            "impact_summary": job.impact_summary,
        }
        if job.replacement_task_ids:
            skipped.append({**item, "reason": "superseded"})
        elif not job.controllable:
            skipped.append({**item, "reason": "read_only"})
        else:
            installed = cron_installed(job)
            if operation == "pause" and job.group_pause_protected and scope == "group":
                skipped.append({**item, "reason": "protected_controller"})
            elif operation == "pause" and not installed:
                skipped.append({**item, "reason": "already_inactive"})
            elif operation == "resume" and (
                not job.install_script or not job.install_script.exists()
            ):
                skipped.append({**item, "reason": "missing_installer"})
            elif operation == "resume" and installed:
                skipped.append({**item, "reason": "already_active"})
            else:
                actions.append(item)

    for job in codex_jobs:
        item = {
            "task_id": f"codex_automation:{job.key}",
            "kind": "codex",
            "key": job.key,
            "description": job.description,
            "schedule": "disabled",
            "execution_identity": str(job.toml),
            "impact_level": job.impact_level,
            "impact_summary": job.impact_summary,
        }
        if operation == "resume":
            skipped.append({**item, "reason": "non_authoritative"})
        elif codex_status(job) == "ACTIVE":
            actions.append(item)
        else:
            skipped.append({**item, "reason": "already_inactive"})

    actions.sort(key=lambda item: item["task_id"])
    skipped.sort(key=lambda item: item["task_id"])
    issued_at = int(time.time()) if issued_at is None else issued_at
    canonical = {
        "schema_version": 1,
        "scope": scope,
        "group": group,
        "operation": operation,
        "target_kind": target_kind or "",
        "target_key": target_key or "",
        "actions": [
            {
                "kind": item["kind"],
                "key": item["key"],
                "execution_identity": item["execution_identity"],
            }
            for item in actions
        ],
        "issued_at": issued_at,
    }
    digest = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:32]
    return {
        "schema_version": 1,
        "scope": scope,
        "group": group,
        "operation": operation,
        "target_kind": target_kind or "",
        "target_key": target_key or "",
        "plan_id": f"{issued_at}.{digest}",
        "issued_at_unix": issued_at,
        "expires_at_unix": issued_at + CONTROL_PLAN_TTL_SECONDS,
        "requires_confirmation": bool(actions),
        "actions": actions,
        "skipped": skipped,
    }


def apply_control_plan(
    group: str,
    operation: str,
    *,
    confirm_plan: str | None,
    target_kind: str | None = None,
    target_key: str | None = None,
) -> dict:
    """Execute the exact current pause or resume Control Plan.

    Args:
        group: Registered operational group.
        operation: Confirmed operation, either ``pause`` or ``resume``.
        confirm_plan: Short-lived plan ID returned by ``build_control_plan``.
        target_kind: Optional scheduler kind for an item-scoped plan.
        target_key: Optional registered scheduler key for an item-scoped plan.

    Returns:
        The validated Control Plan that was executed.

    Raises:
        ValueError: If confirmation is missing, expired, or no longer current.
    """
    if not confirm_plan:
        raise ValueError("Control Plan confirmation is required")
    try:
        issued_at = int(confirm_plan.split(".", 1)[0])
    except (TypeError, ValueError) as exc:
        raise ValueError("Control Plan confirmation is invalid") from exc
    now = int(time.time())
    if issued_at > now + 5 or now - issued_at > CONTROL_PLAN_TTL_SECONDS:
        raise ValueError("Control Plan confirmation is expired")

    if target_kind:
        plan = build_control_plan(
            group,
            operation,
            issued_at=issued_at,
            target_kind=target_kind,
            target_key=target_key,
        )
    else:
        plan = build_control_plan(group, operation, issued_at=issued_at)
    if not hmac.compare_digest(plan["plan_id"], confirm_plan):
        raise ValueError("Control Plan confirmation is stale")
    for action in plan["actions"]:
        if operation == "resume":
            if action["kind"] == "launchd":
                resume_launchd(find_launchd(action["key"]))
            elif action["kind"] == "cron":
                job = find_cron(action["key"])
                if job.install_script and job.install_script.exists():
                    run([str(job.install_script), "install"])
        elif action["kind"] == "launchd":
            pause_launchd(find_launchd(action["key"]))
        elif action["kind"] == "cron":
            remove_cron_job(find_cron(action["key"]))
        elif action["kind"] == "codex":
            set_codex_status(find_codex(action["key"]), "PAUSED")
    if not target_kind:
        print_status(group)
    return plan


def pause_one(kind: str, key: str, *, confirm_plan: str | None = None) -> None:
    if kind == "launchd":
        job = find_launchd(key)
        if not job.controllable:
            raise KeyError(f"read-only launchd job: {key}")
        if job.impact_level == "critical":
            apply_control_plan(
                "all",
                "pause",
                confirm_plan=confirm_plan,
                target_kind=kind,
                target_key=key,
            )
            return
        pause_launchd(job)
    elif kind == "cron":
        job = find_cron(key)
        if not job.controllable:
            raise KeyError(f"read-only cron job: {key}")
        if job.impact_level == "critical":
            apply_control_plan(
                "all",
                "pause",
                confirm_plan=confirm_plan,
                target_kind=kind,
                target_key=key,
            )
            return
        remove_cron_job(job)
    elif kind == "codex":
        job = find_codex(key)
        if job.impact_level == "critical":
            apply_control_plan(
                "all",
                "pause",
                confirm_plan=confirm_plan,
                target_kind=kind,
                target_key=key,
            )
            return
        set_codex_status(job, "PAUSED")
    else:
        raise KeyError(f"unsupported item type: {kind}")


def resume_one(kind: str, key: str) -> None:
    if kind == "launchd":
        job = find_launchd(key)
        if not job.controllable or job.replacement_task_ids:
            raise KeyError(f"read-only launchd job: {key}")
        resume_launchd(job)
    elif kind == "cron":
        job = find_cron(key)
        if not job.controllable or job.replacement_task_ids:
            raise KeyError(f"read-only cron job: {key}")
        if job.install_script and job.install_script.exists():
            run([str(job.install_script), "install"])
    elif kind == "codex":
        find_codex(key)
        raise KeyError(f"Codex automation cannot own scheduling: {key}")
    else:
        raise KeyError(f"unsupported item type: {kind}")


def retire_one(kind: str, key: str) -> bool:
    """Remove one explicitly superseded scheduler entry.

    Args:
        kind: Scheduler kind. Only ``cron`` is currently supported.
        key: Registered scheduler key.

    Returns:
        Whether an installed entry was removed.

    Raises:
        KeyError: If the entry is not a registered superseded scheduler.
    """
    if kind != "cron":
        raise KeyError(f"unsupported retire item type: {kind}")
    job = find_cron(key)
    if not job.replacement_task_ids:
        raise KeyError(f"scheduler is not superseded: {key}")
    return remove_cron_job(job)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Control DC-Agent watchdog/scheduled jobs."
    )
    parser.add_argument(
        "command",
        choices=(
            "status",
            "pause",
            "plan-pause",
            "plan-pause-one",
            "plan-resume",
            "resume",
            "pause-one",
            "resume-one",
            "retire-one",
            "plan-review",
            "review",
        ),
    )
    parser.add_argument(
        "group",
        nargs="?",
        default="all",
        help="group for status/pause/resume, or item type for item operations",
    )
    parser.add_argument("key", nargs="?", help="item key for pause-one/resume-one")
    parser.add_argument(
        "--json", action="store_true", help="print machine-readable JSON"
    )
    parser.add_argument(
        "--confirm-plan",
        help="plan_id returned by a plan command; required for protected actions",
    )
    args = parser.parse_args(argv)
    if args.command == "status":
        if args.json:
            print(json.dumps(collect_status(args.group), ensure_ascii=False))
        else:
            print_status(args.group)
    elif args.command == "plan-review":
        if not args.key:
            parser.error("plan-review requires incident ID and review operation")
        try:
            plan = _load_repair_engine().build_review_control_plan(
                state_path=DC_ROOT / "data" / "watchdog" / "repair_state.json",
                incident_id=args.group,
                operation=args.key,
            )
        except ValueError as exc:
            parser.error(str(exc))
        if args.json:
            print(json.dumps(plan, ensure_ascii=False))
        else:
            print(
                f"Review Control Plan {plan['plan_id']}: "
                f"{plan['incident_id']} {plan['from_status']} -> {plan['to_status']}"
            )
    elif args.command == "review":
        if not args.key:
            parser.error("review requires incident ID and review operation")
        if not args.confirm_plan:
            parser.error("review requires --confirm-plan")
        try:
            result = _load_repair_engine().apply_review_control_plan(
                state_path=DC_ROOT / "data" / "watchdog" / "repair_state.json",
                incident_id=args.group,
                operation=args.key,
                confirm_plan=args.confirm_plan,
            )
        except ValueError as exc:
            parser.error(str(exc))
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        else:
            print(
                f"review {result['incident_id']}: "
                f"{result['from_status']} -> {result['to_status']}"
            )
    elif args.command in {"plan-pause", "plan-resume"}:
        operation = args.command.removeprefix("plan-")
        plan = build_control_plan(args.group, operation)
        if args.json:
            print(json.dumps(plan, ensure_ascii=False))
        else:
            print(f"Control Plan {plan['plan_id']} ({operation} {args.group})")
            for action in plan["actions"]:
                print(f"- {action['task_id']}: {action['description']}")
    elif args.command == "plan-pause-one":
        if not args.key:
            parser.error("plan-pause-one requires item type and key")
        try:
            plan = build_control_plan(
                "all",
                "pause",
                target_kind=args.group,
                target_key=args.key,
            )
        except (KeyError, ValueError) as exc:
            parser.error(str(exc))
        if args.json:
            print(json.dumps(plan, ensure_ascii=False))
        else:
            print(f"Control Plan {plan['plan_id']} (pause {args.group}:{args.key})")
            for action in plan["actions"]:
                print(f"- {action['task_id']}: {action['description']}")
    elif args.command in {"pause", "resume"}:
        if not args.confirm_plan:
            parser.error(
                f"group {args.command} requires --confirm-plan; "
                f"run plan-{args.command} first"
            )
        try:
            apply_control_plan(
                args.group,
                args.command,
                confirm_plan=args.confirm_plan,
            )
        except ValueError as exc:
            parser.error(str(exc))
    elif args.command == "pause-one":
        if not args.key:
            parser.error("pause-one requires item type and key")
        try:
            pause_one(args.group, args.key, confirm_plan=args.confirm_plan)
        except ValueError as exc:
            parser.error(str(exc))
    elif args.command == "resume-one":
        if not args.key:
            parser.error("resume-one requires item type and key")
        resume_one(args.group, args.key)
    elif args.command == "retire-one":
        if not args.key:
            parser.error("retire-one requires item type and key")
        removed = retire_one(args.group, args.key)
        print("retired" if removed else "already retired")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
