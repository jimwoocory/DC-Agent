#!/usr/bin/env python3
"""Cron tick for NAS-backed company knowledge ingestion.

This script is intentionally a scheduler, not a long-running daemon. The main
DC-Agent watchdog calls ``--tick`` every minute; due jobs are started in the
background and each worker records its own result in data/watchdog.
"""

from __future__ import annotations

import argparse
import ast
import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DC_ROOT = Path("/Users/dianchi/DC-Agent")
WD_ROOT = DC_ROOT / "data" / "watchdog"
LOCK_ROOT = WD_ROOT / "locks"
FEISHU_CLOUD_RUN_LOCK_DIR = LOCK_ROOT / "feishu-cloud-workflow-run.lock"
FEISHU_CLOUD_PAUSE_PATH = WD_ROOT / "feishu_cloud_workflow.pause"
STATE_PATH = WD_ROOT / "knowledge_cycle_state.json"
STATE_LOCK_PATH = WD_ROOT / "knowledge_cycle_state.lock"
LOG_PATH = WD_ROOT / "knowledge_cycle.log"
ENV_PATH = Path.home() / ".dc-agent.env"

VENV_PYTHON = DC_ROOT / ".venv" / "bin" / "python"
PYTHON = VENV_PYTHON if VENV_PYTHON.exists() else Path(sys.executable)

STEP_CONFIG: dict[str, dict[str, Any]] = {
    "mount": {
        "interval_sec": int(os.getenv("KNOWLEDGE_MOUNT_INTERVAL_SEC", "60")),
        "timeout_sec": int(os.getenv("KNOWLEDGE_MOUNT_TIMEOUT_SEC", "30")),
        "max_runtime_sec": 120,
    },
    "kb_inbox": {
        "interval_sec": int(os.getenv("KNOWLEDGE_KB_INBOX_INTERVAL_SEC", "900")),
        "timeout_sec": int(os.getenv("KNOWLEDGE_KB_INBOX_TIMEOUT_SEC", "900")),
        "max_runtime_sec": 1800,
        "enabled": os.getenv("KNOWLEDGE_ENABLE_ASTRBOT_MIRROR", "0") == "1",
    },
    "dc_memory": {
        "interval_sec": int(os.getenv("KNOWLEDGE_DC_MEMORY_INTERVAL_SEC", "900")),
        "timeout_sec": int(os.getenv("KNOWLEDGE_DC_MEMORY_TIMEOUT_SEC", "900")),
        "max_runtime_sec": 1800,
        "move_success": os.getenv("KNOWLEDGE_DC_MEMORY_MOVE_SUCCESS", "0") == "1",
        "enabled": os.getenv("KNOWLEDGE_ENABLE_PERIODIC_DC_MEMORY", "0") == "1",
    },
    "obsidian_refs": {
        "interval_sec": int(os.getenv("KNOWLEDGE_OBSIDIAN_REFS_INTERVAL_SEC", "3600")),
        "timeout_sec": int(os.getenv("KNOWLEDGE_OBSIDIAN_REFS_TIMEOUT_SEC", "900")),
        "max_runtime_sec": 1800,
        "enabled": os.getenv("KNOWLEDGE_ENABLE_OBSIDIAN_REFS", "1") == "1",
    },
    "obsidian_governance_export": {
        "interval_sec": int(
            os.getenv("KNOWLEDGE_OBSIDIAN_GOVERNANCE_EXPORT_INTERVAL_SEC", "3600")
        ),
        "timeout_sec": int(
            os.getenv("KNOWLEDGE_OBSIDIAN_GOVERNANCE_EXPORT_TIMEOUT_SEC", "300")
        ),
        "max_runtime_sec": 900,
        "enabled": os.getenv("KNOWLEDGE_ENABLE_OBSIDIAN_GOVERNANCE_EXPORT", "0") == "1",
        "limit": int(os.getenv("KNOWLEDGE_OBSIDIAN_GOVERNANCE_EXPORT_LIMIT", "50")),
    },
    "obsidian_governance_import": {
        "interval_sec": int(
            os.getenv("KNOWLEDGE_OBSIDIAN_GOVERNANCE_IMPORT_INTERVAL_SEC", "3600")
        ),
        "timeout_sec": int(
            os.getenv("KNOWLEDGE_OBSIDIAN_GOVERNANCE_IMPORT_TIMEOUT_SEC", "300")
        ),
        "max_runtime_sec": 900,
        "enabled": os.getenv("KNOWLEDGE_ENABLE_OBSIDIAN_GOVERNANCE_IMPORT", "0") == "1",
    },
    "obsidian_governance_promote": {
        "interval_sec": int(
            os.getenv("KNOWLEDGE_OBSIDIAN_GOVERNANCE_PROMOTE_INTERVAL_SEC", "3600")
        ),
        "timeout_sec": int(
            os.getenv("KNOWLEDGE_OBSIDIAN_GOVERNANCE_PROMOTE_TIMEOUT_SEC", "300")
        ),
        "max_runtime_sec": 900,
        "enabled": os.getenv("KNOWLEDGE_ENABLE_OBSIDIAN_GOVERNANCE_PROMOTE", "0")
        == "1",
        "dry_run": os.getenv("KNOWLEDGE_OBSIDIAN_GOVERNANCE_PROMOTE_DRY_RUN", "1")
        == "1",
    },
    "feishu_nas_workflow": {
        "interval_sec": int(
            os.getenv("KNOWLEDGE_FEISHU_NAS_WORKFLOW_INTERVAL_SEC", "600")
        ),
        "timeout_sec": int(
            os.getenv("KNOWLEDGE_FEISHU_NAS_WORKFLOW_TIMEOUT_SEC", "1800")
        ),
        "max_runtime_sec": 2400,
        "enabled": os.getenv("KNOWLEDGE_ENABLE_FEISHU_NAS_WORKFLOW", "1") == "1",
        "attachment_limit": int(
            os.getenv("KNOWLEDGE_FEISHU_CLOUD_ATTACHMENT_LIMIT", "20")
        ),
    },
    "kb_reconcile": {
        "interval_sec": int(os.getenv("KNOWLEDGE_KB_RECONCILE_INTERVAL_SEC", "900")),
        "timeout_sec": int(os.getenv("KNOWLEDGE_KB_RECONCILE_TIMEOUT_SEC", "60")),
        "max_runtime_sec": 300,
    },
    "feishu_repair": {
        "interval_sec": int(os.getenv("KNOWLEDGE_FEISHU_REPAIR_INTERVAL_SEC", "3600")),
        "timeout_sec": int(os.getenv("KNOWLEDGE_FEISHU_REPAIR_TIMEOUT_SEC", "1800")),
        "max_runtime_sec": 2400,
        "stale_sec": int(os.getenv("KNOWLEDGE_FEISHU_STALE_SEC", "4000")),
        "enabled": os.getenv("KNOWLEDGE_ENABLE_LEGACY_FEISHU_REPAIR", "0") == "1",
    },
    "daily_full": {
        "interval_sec": int(os.getenv("KNOWLEDGE_DAILY_FULL_INTERVAL_SEC", "86400")),
        "timeout_sec": int(os.getenv("KNOWLEDGE_DAILY_FULL_TIMEOUT_SEC", "3600")),
        "max_runtime_sec": 7200,
        "enabled": os.getenv("KNOWLEDGE_ENABLE_DAILY_FULL_SCAN", "0") == "1",
    },
}


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def ensure_dirs() -> None:
    WD_ROOT.mkdir(parents=True, exist_ok=True)
    LOCK_ROOT.mkdir(parents=True, exist_ok=True)


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"steps": {}}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"steps": {}}


def save_state(state: dict[str, Any]) -> None:
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


@contextmanager
def locked_state():
    ensure_dirs()
    with STATE_LOCK_PATH.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        state = load_state()
        try:
            yield state
            save_state(state)
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def update_step(step: str, **fields: Any) -> None:
    with locked_state() as state:
        steps = state.setdefault("steps", {})
        entry = steps.setdefault(step, {})
        entry.update(fields)
        state["updated_at"] = now_iso()


def append_log(message: str) -> None:
    ensure_dirs()
    with LOG_PATH.open("a", encoding="utf-8") as log_file:
        log_file.write(f"{now_iso()} {message}\n")


def recent_log_lines(limit: int = 300) -> list[str]:
    if not LOG_PATH.exists():
        return []
    try:
        return LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()[
            -limit:
        ]
    except OSError:
        return []


def latest_dict_after(marker: str, lines: list[str]) -> dict[str, Any]:
    for line in reversed(lines):
        if marker not in line:
            continue
        raw = line.split(marker, 1)[1].strip()
        try:
            return ast.literal_eval(raw)
        except (SyntaxError, ValueError):
            return {}
    return {}


def latest_json_line(lines: list[str], keys: set[str]) -> dict[str, Any]:
    for line in reversed(lines):
        start = line.find("{")
        if start < 0:
            continue
        try:
            data = json.loads(line[start:])
        except json.JSONDecodeError:
            continue
        if keys.issubset(data.keys()):
            return data
    return {}


def has_new_feishu_downloads() -> bool:
    stats = latest_dict_after("同步统计:", recent_log_lines())
    return any(int(stats.get(key) or 0) > 0 for key in ("synced", "attachment_synced"))


def latest_memory_indexed_count() -> int:
    stats = latest_json_line(
        recent_log_lines(), {"scanned", "indexed", "failed", "quarantined"}
    )
    try:
        return int(stats.get("indexed") or 0)
    except (TypeError, ValueError):
        return 0


def latest_targeted_memory_stats(lines: list[str]) -> dict[str, Any]:
    for line in reversed(lines):
        start = line.find("{")
        if start < 0:
            continue
        try:
            data = json.loads(line[start:])
        except json.JSONDecodeError:
            continue
        targeted = data.get("targeted_memory")
        if not isinstance(targeted, dict):
            continue
        return {
            "scanned": int(targeted.get("scanned") or 0),
            "indexed": int(targeted.get("indexed") or 0),
            "skipped": 0,
            "failed": int(targeted.get("failed") or 0),
            "quarantined": 0,
            "mode": "targeted_paths",
        }
    return {}


def status_snapshot() -> dict[str, Any]:
    ensure_dirs()
    state = load_state()
    lines = recent_log_lines()
    latest_sync = latest_dict_after("同步统计:", lines)
    latest_memory = latest_targeted_memory_stats(lines) or latest_json_line(
        lines,
        {"scanned", "indexed", "failed", "quarantined"},
    )
    latest_attachment = latest_dict_after("表格附件同步完成:", lines)
    return {
        "updated_at": now_iso(),
        "steps": state.get("steps", {}),
        "last_tick_at": state.get("last_tick_at", ""),
        "last_tick_scheduled": state.get("last_tick_scheduled", []),
        "running": {
            step: is_running(step)
            for step in (
                "feishu_nas_workflow",
                "dc_memory",
                "obsidian_refs",
                "obsidian_governance_export",
                "obsidian_governance_import",
                "obsidian_governance_promote",
                "mount",
            )
            if step in STEP_CONFIG
        },
        "latest_sync_stats": latest_sync,
        "latest_attachment_stats": latest_attachment,
        "latest_dc_memory_stats": latest_memory,
        "log_path": str(LOG_PATH),
    }


def load_dotenv_env() -> dict[str, str]:
    """Return subprocess env with local DC-Agent secrets loaded if present."""
    env = os.environ.copy()
    if not ENV_PATH.exists():
        return env
    try:
        for raw_line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export ") :].strip()
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                env.setdefault(key, value)
    except OSError as exc:
        append_log(f"ENV_LOAD_WARN path={ENV_PATH} error={exc}")
    return env


def step_lock_dir(step: str) -> Path:
    return LOCK_ROOT / f"knowledge-{step}.lock"


def is_running(step: str) -> bool:
    lock_dir = step_lock_dir(step)
    if not lock_dir.exists():
        return False

    max_runtime = int(STEP_CONFIG[step]["max_runtime_sec"])
    age = time.time() - lock_dir.stat().st_mtime
    if age > max_runtime:
        shutil.rmtree(lock_dir, ignore_errors=True)
        append_log(f"STALE_LOCK step={step} age={int(age)}s removed")
        update_step(step, status="stale_lock_removed", lock_age_sec=int(age))
        return False
    return True


def is_due(step: str) -> bool:
    interval = int(STEP_CONFIG[step]["interval_sec"])
    with locked_state() as state:
        entry = state.setdefault("steps", {}).setdefault(step, {})
        last_started = float(entry.get("last_started_ts") or 0)
    return time.time() - last_started >= interval


def file_age_sec(path: Path) -> int | None:
    try:
        return int(time.time() - path.stat().st_mtime)
    except OSError:
        return None


def feishu_needs_repair() -> bool:
    log_age = file_age_sec(DC_ROOT / "nas_sync" / "feishu_sync.log")
    stale_sec = int(STEP_CONFIG["feishu_repair"]["stale_sec"])
    if log_age is None:
        return True
    if log_age > stale_sec:
        return True
    update_step(
        "feishu_repair",
        status="ok",
        reason="external_launchd_fresh",
        log_age_sec=log_age,
        checked_at=now_iso(),
    )
    return False


def feishu_cloud_workflow_running() -> bool:
    return is_running("feishu_nas_workflow") or FEISHU_CLOUD_RUN_LOCK_DIR.exists()


def feishu_cloud_workflow_paused() -> bool:
    return FEISHU_CLOUD_PAUSE_PATH.exists()


def start_step(step: str) -> bool:
    if is_running(step):
        return False

    cmd = [str(PYTHON), str(Path(__file__).resolve()), "--run-step", step]
    proc = subprocess.Popen(
        cmd,
        cwd=str(DC_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    now_ts = time.time()
    update_step(
        step,
        status="scheduled",
        pid=proc.pid,
        last_started_ts=now_ts,
        last_started_at=now_iso(),
    )
    append_log(f"SCHEDULE step={step} pid={proc.pid}")
    return True


def command_for_step(step: str) -> list[str] | None:
    if step == "mount":
        return ["/bin/bash", str(DC_ROOT / "nas_sync" / "watchdog.sh")]
    if step == "kb_inbox":
        if not STEP_CONFIG["kb_inbox"]["enabled"]:
            return None
        return [
            str(PYTHON),
            str(DC_ROOT / "nas_sync" / "watcher.py"),
            "--once",
            "--inbox-only",
        ]
    if step == "dc_memory":
        cmd = [
            str(PYTHON),
            str(DC_ROOT / "nas_sync" / "dc_memory_indexer.py"),
            "--once",
            "--inbox-only",
        ]
        if not STEP_CONFIG["dc_memory"]["move_success"]:
            cmd.append("--no-move")
        return cmd
    if step == "obsidian_refs":
        if not STEP_CONFIG["obsidian_refs"]["enabled"]:
            return None
        return [
            str(PYTHON),
            str(DC_ROOT / "scripts-company" / "generate_obsidian_refs.py"),
        ]
    if step == "obsidian_governance_export":
        if not STEP_CONFIG["obsidian_governance_export"]["enabled"]:
            return None
        return [
            str(PYTHON),
            str(DC_ROOT / "scripts-tools" / "obsidian_memory_governance.py"),
            "export",
            "--limit",
            str(STEP_CONFIG["obsidian_governance_export"]["limit"]),
        ]
    if step == "obsidian_governance_import":
        if not STEP_CONFIG["obsidian_governance_import"]["enabled"]:
            return None
        return [
            str(PYTHON),
            str(DC_ROOT / "scripts-tools" / "obsidian_memory_governance.py"),
            "import",
            "--actor",
            "knowledge_cycle",
        ]
    if step == "obsidian_governance_promote":
        if not STEP_CONFIG["obsidian_governance_promote"]["enabled"]:
            return None
        cmd = [
            str(PYTHON),
            str(DC_ROOT / "scripts-tools" / "obsidian_memory_governance.py"),
            "promote",
            "--actor",
            "knowledge_cycle",
        ]
        if STEP_CONFIG["obsidian_governance_promote"]["dry_run"]:
            cmd.append("--dry-run")
        return cmd
    if step == "feishu_nas_workflow":
        if not STEP_CONFIG["feishu_nas_workflow"]["enabled"]:
            return None
        return [
            str(PYTHON),
            str(DC_ROOT / "scripts-watchdog" / "feishu_cloud_workflow.py"),
            "--run-next",
            "--learn",
            "--attachment-limit",
            str(STEP_CONFIG["feishu_nas_workflow"]["attachment_limit"]),
        ]
    if step == "feishu_repair":
        if not STEP_CONFIG["feishu_repair"]["enabled"]:
            return None
        return [str(PYTHON), str(DC_ROOT / "nas_sync" / "feishu_sync.py")]
    if step == "kb_reconcile":
        return [
            str(PYTHON),
            str(DC_ROOT / "nas_sync" / "dedupe_admin.py"),
            "kb-reconcile",
        ]
    if step == "daily_full":
        if not STEP_CONFIG["daily_full"]["enabled"]:
            return None
        return [
            str(PYTHON),
            str(DC_ROOT / "nas_sync" / "watcher.py"),
            "--once",
            "--full-scan",
        ]
    raise ValueError(f"unknown step: {step}")


def run_step(step: str) -> int:
    if step not in STEP_CONFIG:
        print(f"unknown step: {step}", file=sys.stderr)
        return 2

    lock_dir = step_lock_dir(step)
    try:
        lock_dir.mkdir(parents=True)
    except FileExistsError:
        append_log(f"SKIP_RUNNING step={step}")
        return 0

    started = time.time()
    started_at = now_iso()
    update_step(
        step,
        status="running",
        pid=os.getpid(),
        last_started_ts=started,
        last_started_at=started_at,
        run_started_at=started_at,
    )
    append_log(f"START step={step}")

    try:
        cmd = command_for_step(step)
        if cmd is None:
            update_step(
                step,
                status="disabled",
                run_finished_at=now_iso(),
                duration_sec=round(time.time() - started, 3),
            )
            append_log(f"DISABLED step={step}")
            return 0

        timeout = int(STEP_CONFIG[step]["timeout_sec"])
        with LOG_PATH.open("a", encoding="utf-8") as log_file:
            log_file.write(f"{now_iso()} COMMAND step={step} cmd={' '.join(cmd)}\n")
            result = subprocess.run(
                cmd,
                cwd=str(DC_ROOT),
                env=load_dotenv_env(),
                stdout=log_file,
                stderr=log_file,
                timeout=timeout,
                check=False,
            )

        status = "ok" if result.returncode == 0 else "fail"
        update_step(
            step,
            status=status,
            exit_code=result.returncode,
            run_finished_at=now_iso(),
            duration_sec=round(time.time() - started, 3),
        )
        append_log(f"FINISH step={step} status={status} exit_code={result.returncode}")
        if step == "dc_memory":
            indexed_count = latest_memory_indexed_count()
            if not STEP_CONFIG["obsidian_refs"]["enabled"]:
                append_log("POST_MEMORY_OBSIDIAN skip=disabled")
            elif indexed_count <= 0:
                append_log(
                    f"POST_MEMORY_OBSIDIAN skip=no_new_indexed indexed={indexed_count}"
                )
            elif is_running("obsidian_refs"):
                append_log("POST_MEMORY_OBSIDIAN skip=obsidian_refs_already_running")
            else:
                append_log(
                    f"POST_MEMORY_OBSIDIAN start=obsidian_refs indexed={indexed_count}"
                )
                run_step("obsidian_refs")
        if step == "feishu_nas_workflow" and result.returncode == 0:
            append_log(
                "POST_FEISHU_LEARN step=feishu_nas_workflow skip=handled_by_cloud_workflow"
            )
        if step == "feishu_repair" and result.returncode == 0:
            if is_running("dc_memory"):
                append_log(
                    f"POST_FEISHU_LEARN step={step} skip=dc_memory_already_running"
                )
            elif not has_new_feishu_downloads():
                append_log(f"POST_FEISHU_LEARN step={step} skip=no_new_downloads")
            else:
                append_log(f"POST_FEISHU_LEARN step={step} start=dc_memory")
                run_step("dc_memory")
            if STEP_CONFIG["kb_inbox"]["enabled"]:
                if is_running("kb_inbox"):
                    append_log(
                        f"POST_FEISHU_ASTRBOT_MIRROR step={step} skip=kb_inbox_already_running"
                    )
                else:
                    append_log(f"POST_FEISHU_ASTRBOT_MIRROR step={step} start=kb_inbox")
                    run_step("kb_inbox")
        return result.returncode
    except subprocess.TimeoutExpired:
        update_step(
            step,
            status="timeout",
            run_finished_at=now_iso(),
            duration_sec=round(time.time() - started, 3),
        )
        append_log(f"TIMEOUT step={step}")
        return 124
    finally:
        shutil.rmtree(lock_dir, ignore_errors=True)


def tick() -> int:
    ensure_dirs()
    scheduled: list[str] = []

    if feishu_cloud_workflow_running():
        append_log("SKIP_DUE step=mount reason=feishu_nas_workflow_running")
    elif is_due("mount") and start_step("mount"):
        scheduled.append("mount")

    if STEP_CONFIG["dc_memory"]["enabled"]:
        if feishu_cloud_workflow_running():
            append_log("SKIP_DUE step=dc_memory reason=feishu_nas_workflow_running")
        elif feishu_cloud_workflow_paused():
            append_log("SKIP_DUE step=dc_memory reason=feishu_nas_workflow_paused")
            update_step(
                "dc_memory",
                status="paused",
                reason="feishu_nas_workflow_paused",
                checked_at=now_iso(),
            )
        elif is_due("dc_memory") and start_step("dc_memory"):
            scheduled.append("dc_memory")
    else:
        update_step(
            "dc_memory",
            status="disabled",
            reason="periodic_dc_memory_disabled",
            checked_at=now_iso(),
        )

    if not STEP_CONFIG["obsidian_refs"]["enabled"]:
        update_step("obsidian_refs", status="disabled", checked_at=now_iso())

    for step in (
        "obsidian_governance_export",
        "obsidian_governance_import",
        "obsidian_governance_promote",
    ):
        if STEP_CONFIG[step]["enabled"]:
            if is_due(step) and start_step(step):
                scheduled.append(step)
        else:
            update_step(step, status="disabled", checked_at=now_iso())

    if STEP_CONFIG["feishu_nas_workflow"]["enabled"]:
        if is_due("feishu_nas_workflow") and start_step("feishu_nas_workflow"):
            scheduled.append("feishu_nas_workflow")
    else:
        update_step("feishu_nas_workflow", status="disabled", checked_at=now_iso())

    if STEP_CONFIG["kb_inbox"]["enabled"]:
        for step in ("kb_inbox", "kb_reconcile"):
            if is_due(step) and start_step(step):
                scheduled.append(step)
    else:
        update_step(
            "kb_inbox",
            status="disabled",
            reason="astrbot_mirror_disabled",
            checked_at=now_iso(),
        )
        update_step(
            "kb_reconcile",
            status="disabled",
            reason="astrbot_mirror_disabled",
            checked_at=now_iso(),
        )

    if (
        STEP_CONFIG["feishu_repair"]["enabled"]
        and is_due("feishu_repair")
        and feishu_needs_repair()
    ):
        if start_step("feishu_repair"):
            scheduled.append("feishu_repair")
    elif not STEP_CONFIG["feishu_repair"]["enabled"]:
        update_step(
            "feishu_repair",
            status="disabled",
            reason="legacy_feishu_repair_disabled",
            checked_at=now_iso(),
        )

    if STEP_CONFIG["daily_full"]["enabled"] and is_due("daily_full"):
        if start_step("daily_full"):
            scheduled.append("daily_full")
    elif not STEP_CONFIG["daily_full"]["enabled"]:
        update_step("daily_full", status="disabled", checked_at=now_iso())

    with locked_state() as state:
        state["last_tick_at"] = now_iso()
        state["last_tick_scheduled"] = scheduled
    append_log(f"TICK scheduled={','.join(scheduled) if scheduled else '-'}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="DC-Agent knowledge watchdog cycle")
    parser.add_argument("--tick", action="store_true", help="run one scheduler tick")
    parser.add_argument("--run-step", choices=sorted(STEP_CONFIG), help="run one step")
    parser.add_argument(
        "--status", action="store_true", help="print workflow status snapshot"
    )
    args = parser.parse_args()

    if args.status:
        print(json.dumps(status_snapshot(), ensure_ascii=False, indent=2))
        return 0
    if args.run_step:
        return run_step(args.run_step)
    return tick()


if __name__ == "__main__":
    raise SystemExit(main())
