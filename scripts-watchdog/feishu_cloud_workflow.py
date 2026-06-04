#!/usr/bin/env python3
"""Feishu cloud-space size-ordered queue/status helper.

The user sorts the Feishu admin cloud-space page by Size. This workflow keeps
that displayed size order as the queue order, while still using each row token
or verified URL as the stable identity for sync and memory indexing.
"""

from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DC_ROOT = Path("/Users/dianchi/DC-Agent")
TARGETS_PATH = DC_ROOT / "data" / "watchdog" / "feishu_cloud_targets.json"
FEISHU_STATE_PATH = DC_ROOT / "nas_sync" / "logs" / "sync_state.json"
MEMORY_STATE_PATH = DC_ROOT / "data" / "watchdog" / "dc_memory_state.json"
NAS_MEMORY_DB = DC_ROOT / "data" / "nas_memory.db"
HARNESS_MEMORY_DB = DC_ROOT / "data" / "harness_memory.db"
NAS_ROOT = Path("/Users/dianchi/nas_kb")
INBOX = NAS_ROOT / "inbox"
FAILED = NAS_ROOT / "failed"
PYTHON = DC_ROOT / ".venv" / "bin" / "python"
ENV_PATH = Path.home() / ".dc-agent.env"
RUN_LOCK_DIR = (
    DC_ROOT / "data" / "watchdog" / "locks" / "feishu-cloud-workflow-run.lock"
)
RUN_LOCK_PID = RUN_LOCK_DIR / "pid"
RUN_LOCK_STALE_AFTER_SECONDS = 60
RUN_PAUSE_PATH = DC_ROOT / "data" / "watchdog" / "feishu_cloud_workflow.pause"


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return default


def save_targets(data: dict[str, Any]) -> None:
    data["updated_at"] = now_iso()
    tmp = TARGETS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(TARGETS_PATH)


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_dotenv_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("DC_AGENT_ROOT", str(DC_ROOT))
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
    except OSError:
        return env
    return env


def process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stale_run_lock_reason() -> str:
    if not RUN_LOCK_DIR.exists():
        return ""
    try:
        pid_text = RUN_LOCK_PID.read_text(encoding="utf-8").strip()
    except OSError:
        pid_text = ""
    if pid_text:
        try:
            pid = int(pid_text)
        except ValueError:
            return "invalid_pid"
        return "" if process_alive(pid) else f"dead_pid:{pid}"

    try:
        age = time.time() - RUN_LOCK_DIR.stat().st_mtime
    except OSError:
        return ""
    if age >= RUN_LOCK_STALE_AFTER_SECONDS:
        return f"empty_lock_age_seconds:{int(age)}"
    return ""


def clear_stale_run_lock(reason: str) -> None:
    if not reason or not RUN_LOCK_DIR.exists():
        return
    try:
        if RUN_LOCK_PID.exists():
            RUN_LOCK_PID.unlink()
        RUN_LOCK_DIR.rmdir()
        print(
            json.dumps({"stale_lock_cleared": 1, "reason": reason}, ensure_ascii=False)
        )
    except OSError:
        pass


def acquire_run_lock() -> bool:
    while True:
        try:
            RUN_LOCK_DIR.mkdir(parents=True)
            RUN_LOCK_PID.write_text(str(os.getpid()), encoding="utf-8")
            return True
        except FileExistsError:
            reason = stale_run_lock_reason()
            if reason:
                clear_stale_run_lock(reason)
                continue
            return False


def release_run_lock() -> None:
    try:
        pid_text = RUN_LOCK_PID.read_text(encoding="utf-8").strip()
    except OSError:
        pid_text = ""
    if pid_text and pid_text != str(os.getpid()):
        return
    try:
        if RUN_LOCK_PID.exists():
            RUN_LOCK_PID.unlink()
        RUN_LOCK_DIR.rmdir()
    except OSError:
        pass


def token_from_url(url: str) -> str:
    match = re.search(r"/(?:wiki|docx|doc|sheets|base)/([A-Za-z0-9_-]+)", url)
    return match.group(1) if match else ""


def load_targets() -> dict[str, Any]:
    data = load_json(TARGETS_PATH, {})
    data.setdefault("version", 1)
    data.setdefault("targets", [])
    return data


def target_size_rank(target: dict[str, Any]) -> int:
    try:
        return int(target.get("size_rank") or 999999)
    except (TypeError, ValueError):
        return 999999


def ordered_targets(data: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(data.get("targets", []), key=target_size_rank)


def normalize_admin_type(value: str) -> str:
    raw = value.strip().lower()
    mapping = {
        "电子表格": "sheet",
        "多维表格": "bitable",
        "文档": "docx",
        "幻灯片": "slides",
        "演示文稿": "slides",
        "上传文件": "file",
        "sheet": "sheet",
        "bitable": "bitable",
        "docx": "docx",
        "doc": "doc",
        "slides": "slides",
        "ppt": "slides",
        "pptx": "slides",
        "file": "file",
    }
    return mapping.get(raw, raw)


def safe_path_part(value: str) -> str:
    value = re.sub(r'[\\/:*?"<>|]+', "_", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:160] or "feishu-cloud-item"


def project_folder_link(path: Path, payload: dict[str, Any]) -> Path | None:
    project = str(payload.get("project_name") or "").strip()
    doc_type = str(payload.get("doc_type") or "").strip()
    if not project or not doc_type or not path.exists():
        return None
    base = NAS_ROOT / "projects"
    if str(payload.get("review_status") or "") == "need_review":
        base = base / "_待确认"
    dest = base / safe_path_part(project) / safe_path_part(doc_type) / path.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() or dest.is_symlink():
        try:
            if dest.is_symlink() and Path(os.readlink(dest)) == path:
                return dest
        except OSError:
            pass
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        dest = dest.with_name(f"{dest.stem}.{stamp}{dest.suffix}")
    dest.symlink_to(path)
    return dest


def sync_failure_summary(result: subprocess.CompletedProcess[str]) -> str:
    text = "\n".join(part for part in (result.stderr, result.stdout) if part)
    lines = []
    for line in text.splitlines():
        if "失败:" in line or "failed" in line.lower() or "error=" in line:
            lines.append(line.strip())
    if not lines:
        return f"sync_failed_exit_code={result.returncode}"
    return "\n".join(lines[-5:])


def write_failure_marker(target: dict[str, Any], reason: str) -> str:
    FAILED.mkdir(parents=True, exist_ok=True)
    folder = FAILED / "_cloud_space_export_failures"
    folder.mkdir(parents=True, exist_ok=True)
    rank = str(target.get("size_rank") or "unknown").zfill(4)
    token = str(target.get("token") or target.get("admin_row_id") or "unknown")
    title = safe_path_part(str(target.get("title_hint") or token))
    marker = folder / f"{rank}_{title}__{token[:8]}.error.txt"
    payload = {
        "failed_at": now_iso(),
        "target_id": target.get("id"),
        "size_rank": target.get("size_rank"),
        "size_label": target.get("size_label"),
        "token": token,
        "title": target.get("title_hint"),
        "type_hint": target.get("type_hint"),
        "owner_hint": target.get("owner_hint"),
        "reason": reason,
        "next_action": "飞书导出失败，已隔离；建议人工拆分表格图片/复制为轻量表格/后台另存后重试。",
    }
    marker.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return str(marker)


def modified_label_to_epoch(value: str) -> int:
    value = value.strip()
    if not value:
        return 0
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(value, fmt).replace(tzinfo=UTC).timestamp())
        except ValueError:
            continue
    return 0


def build_sync_cmd(
    target: dict[str, Any],
    python: Path,
    attachment_limit: int,
    *,
    dry_run: bool = False,
) -> list[str] | None:
    url = str(target.get("url") or "")
    base = [
        str(python),
        str(DC_ROOT / "nas_sync" / "feishu_sync.py"),
        "--attachment-limit",
        str(attachment_limit),
    ]
    if url:
        cmd = [*base, "--url", url]
        if dry_run:
            cmd.append("--dry-run")
        return cmd

    token = str(target.get("token") or target.get("admin_row_id") or "")
    ftype = normalize_admin_type(str(target.get("type_hint") or ""))
    name = str(target.get("title_hint") or token or "feishu-cloud-item")
    if not token or not ftype:
        return None
    modified_time = modified_label_to_epoch(str(target.get("modified_at_label") or ""))
    cmd = [
        *base,
        "--token",
        token,
        "--type",
        ftype,
        f"--name={name}",
        "--modified-time",
        str(modified_time),
    ]
    if dry_run:
        cmd.append("--dry-run")
    return cmd


def feishu_state_for(token: str) -> dict[str, Any]:
    state = load_json(FEISHU_STATE_PATH, {"files": {}})
    files = state.get("files") if isinstance(state, dict) else {}
    if not isinstance(files, dict):
        return {}
    if not token:
        return {}
    direct = files.get(token, {})
    if direct:
        return direct
    for item in files.values():
        if isinstance(item, dict) and item.get("admin_token") == token:
            return item
    return {}


def feishu_state_by_name(name: str) -> dict[str, Any]:
    if not name:
        return {}
    state = load_json(FEISHU_STATE_PATH, {"files": {}})
    files = state.get("files") if isinstance(state, dict) else {}
    if not isinstance(files, dict):
        return {}
    for item in files.values():
        if not isinstance(item, dict):
            continue
        local_path = Path(str(item.get("local_path") or ""))
        if item.get("name") == name or local_path.stem == name:
            return item
    return {}


def primary_local_path(target: dict[str, Any], state: dict[str, Any]) -> Path | None:
    raw = str(state.get("local_path") or "")
    if raw and Path(raw).exists():
        return Path(raw)
    local_dir_hint = str(target.get("local_dir_hint") or "").strip()
    if local_dir_hint.endswith("__attachments"):
        candidate = INBOX / f"{local_dir_hint.removesuffix('__attachments')}.xlsx"
        if candidate.exists():
            return candidate
    return None


def install_fragment_paths(path: Path) -> list[Path]:
    try:
        return sorted(path.parent.glob(f".{path.name}.install-*"))
    except OSError:
        return []


def validate_downloaded_state(target: dict[str, Any], state: dict[str, Any]) -> str:
    raw = str(state.get("local_path") or "").strip()
    if not raw:
        return "download_state_missing_local_path"

    path = Path(raw)
    if not path.exists():
        fragments = install_fragment_paths(path)
        detail = ""
        if fragments:
            details = []
            for fragment in fragments[:3]:
                try:
                    details.append(f"{fragment.name}:{fragment.stat().st_size}")
                except OSError:
                    details.append(fragment.name)
            detail = f"; install_fragments={','.join(details)}"
        return f"download_target_missing_after_sync: {path}{detail}"

    try:
        size = path.stat().st_size
    except OSError as exc:
        return f"download_target_stat_failed: {path}: {exc}"
    if size <= 0:
        return f"download_target_empty: {path}"

    suffix = path.suffix.lower()
    if suffix in {".zip", ".docx", ".xlsx", ".pptx"} and not zipfile.is_zipfile(path):
        return f"download_target_invalid_zip_container: {path}"
    return ""


def attachment_manifest(target: dict[str, Any], state: dict[str, Any]) -> Path | None:
    local_dir_hint = str(target.get("local_dir_hint") or "").strip()
    if local_dir_hint:
        path = INBOX / local_dir_hint / ".sync" / "attachments_manifest.csv"
        if path.exists():
            return path

    local_path = Path(str(state.get("local_path") or ""))
    if local_path.exists():
        path = (
            INBOX
            / f"{local_path.stem}__attachments"
            / ".sync"
            / "attachments_manifest.csv"
        )
        if path.exists():
            return path
    return None


def document_exists_for_path(conn: sqlite3.Connection | None, path: str) -> bool:
    if conn is None or not path:
        return False
    try:
        row = conn.execute(
            """
            SELECT 1
            FROM documents
            WHERE source_path = ? OR archive_path = ?
            LIMIT 1
            """,
            (path, path),
        ).fetchone()
    except sqlite3.Error:
        return False
    return bool(row)


def failed_path_exists(local_path: str) -> bool:
    if not local_path:
        return False
    path = Path(local_path)
    try:
        rel = path.relative_to(INBOX)
    except ValueError:
        rel = Path(path.name)
    failed_path = FAILED / rel
    return (
        failed_path.exists()
        or failed_path.with_name(f"{failed_path.name}.error.txt").exists()
    )


def should_quarantine_error(reason: str) -> bool:
    transient_markers = (
        "Input/output error",
        "Operation timed out",
        "Device not configured",
        "Bad file descriptor",
        "No such file or directory",
        "Resource temporarily unavailable",
        "Stale file handle",
    )
    if any(marker in reason for marker in transient_markers):
        return False

    non_retryable_markers = (
        "File is not a zip file",
        "extracted text too short",
        "unsupported extension",
        "文档解析失败",
        "任务完成但无上传记录",
        "文件格式受支持",
        "文件内容未损坏",
    )
    return any(marker in reason for marker in non_retryable_markers)


def quarantine_failed_memory_path(path: Path, reason: str) -> Path | None:
    if not path.exists():
        return None
    try:
        rel = path.relative_to(INBOX)
    except ValueError:
        return None

    dest = FAILED / rel
    if dest.exists():
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        dest = dest.with_name(f"{dest.stem}.{stamp}{dest.suffix}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), str(dest))
    dest.with_name(f"{dest.name}.error.txt").write_text(
        f"failed_at={datetime.now().isoformat(timespec='seconds')}\n"
        f"source={path}\nreason={reason}\n",
        encoding="utf-8",
    )
    return dest


def attachment_stats(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {
            "manifest": "",
            "total": 0,
            "available_only": 0,
            "indexed": 0,
            "quarantined": 0,
            "pending": 0,
            "missing_record": 0,
            "failed": 0,
        }
    stats = {
        "manifest": str(path),
        "total": 0,
        "available_only": 0,
        "indexed": 0,
        "quarantined": 0,
        "pending": 0,
        "missing_record": 0,
        "failed": 0,
    }
    conn: sqlite3.Connection | None = None
    if NAS_MEMORY_DB.exists():
        try:
            conn = sqlite3.connect(NAS_MEMORY_DB)
        except sqlite3.Error:
            conn = None
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        for row in csv.DictReader(file):
            stats["total"] += 1
            status = str(row.get("status") or "").lower()
            local_path = str(row.get("local_path") or "")
            if document_exists_for_path(conn, local_path):
                stats["indexed"] += 1
            elif failed_path_exists(local_path):
                stats["quarantined"] += 1
            elif status in {"synced", "skipped"} and Path(local_path).exists():
                stats["available_only"] += 1
            elif status in {"synced", "skipped", "recorded_missing"}:
                stats["missing_record"] += 1
            elif status == "failed":
                stats["failed"] += 1
            else:
                stats["pending"] += 1
    if conn is not None:
        conn.close()
    return stats


def scalar_query(db_path: Path, sql: str, params: tuple[Any, ...] = ()) -> int:
    if not db_path.exists():
        return 0
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(sql, params).fetchone()
    except sqlite3.Error:
        return 0
    return int(row[0] or 0) if row else 0


def memory_stats(
    target: dict[str, Any], state: dict[str, Any] | None = None
) -> dict[str, Any]:
    state = state or {}
    local_dir_hint = str(target.get("local_dir_hint") or "").strip()
    title_hint = str(target.get("title_hint") or "").strip()
    token = str(target.get("token") or "").strip()
    local_path = str(state.get("local_path") or "").strip()
    if local_path:
        exact_documents = scalar_query(
            NAS_MEMORY_DB,
            "SELECT count(*) FROM documents WHERE source_path = ? OR archive_path = ?",
            (local_path, local_path),
        )
    else:
        exact_documents = 0

    if local_dir_hint:
        like = f"%{local_dir_hint}%"
        fuzzy_documents = scalar_query(
            NAS_MEMORY_DB,
            "SELECT count(*) FROM documents WHERE rel_path LIKE ? OR source_path LIKE ?",
            (like, like),
        )
    elif title_hint:
        like = f"%{title_hint}%"
        fuzzy_documents = scalar_query(
            NAS_MEMORY_DB,
            "SELECT count(*) FROM documents WHERE rel_path LIKE ? OR source_path LIKE ? OR title LIKE ?",
            (like, like, like),
        )
    elif token:
        like = f"%{token}%"
        fuzzy_documents = scalar_query(
            NAS_MEMORY_DB,
            "SELECT count(*) FROM documents WHERE rel_path LIKE ? OR source_path LIKE ?",
            (like, like),
        )
    else:
        fuzzy_documents = 0
    documents = max(exact_documents, fuzzy_documents)
    total_documents = scalar_query(NAS_MEMORY_DB, "SELECT count(*) FROM documents")
    open_reviews = scalar_query(
        NAS_MEMORY_DB,
        "SELECT count(*) FROM review_queue WHERE status = 'open'",
    )
    harness_memories = scalar_query(
        HARNESS_MEMORY_DB,
        "SELECT count(*) FROM harness_memories WHERE memory_kind = 'nas_document'",
    )
    last_state = load_json(MEMORY_STATE_PATH, {})
    return {
        "documents_for_target": documents,
        "total_documents": total_documents,
        "open_review_queue": open_reviews,
        "harness_nas_memories": harness_memories,
        "latest_indexer_stats": last_state.get("last_stats", {})
        if isinstance(last_state, dict)
        else {},
    }


def run_workflow_command(
    name: str, args: list[str], env: dict[str, str]
) -> dict[str, Any]:
    result = subprocess.run(
        args,
        cwd=str(DC_ROOT),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)

    payload: Any = {}
    try:
        payload = json.loads((result.stdout or "{}").strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        payload = {}

    return {
        "name": name,
        "exit_code": result.returncode,
        "payload": payload,
    }


def run_post_memory_convergence(env: dict[str, str]) -> dict[str, Any]:
    python = PYTHON if PYTHON.exists() else Path(sys.executable)
    steps = [
        (
            "subject_owner_corrections",
            [
                str(python),
                str(DC_ROOT / "scripts-company" / "apply_subject_owner_corrections.py"),
                "--apply",
            ],
        ),
        (
            "high_confidence_confirmations",
            [
                str(python),
                str(DC_ROOT / "scripts-company" / "apply_rule_confirmations.py"),
                "--apply",
            ],
        ),
        (
            "tracker_row_confirmations",
            [
                str(python),
                str(DC_ROOT / "scripts-company" / "apply_table_owner_confirmations.py"),
                "--min-confidence",
                "0.78",
                "--min-score",
                "1.0",
                "--apply",
            ],
        ),
        (
            "tracker_document_confirmations",
            [
                str(python),
                str(
                    DC_ROOT
                    / "scripts-company"
                    / "apply_tracker_document_confirmations.py"
                ),
                "--apply",
            ],
        ),
        (
            "obsidian_raw_refs",
            [
                str(python),
                str(DC_ROOT / "scripts-company" / "generate_obsidian_refs.py"),
            ],
        ),
        (
            "obsidian_review_workbench",
            [
                str(python),
                str(DC_ROOT / "scripts-company" / "generate_review_workbench.py"),
            ],
        ),
    ]

    results = [run_workflow_command(name, args, env) for name, args in steps]
    first_failure = next(
        (item["exit_code"] for item in results if int(item["exit_code"] or 0) != 0), 0
    )
    summary = {
        "ran_at": now_iso(),
        "exit_code": int(first_failure or 0),
        "steps": results,
    }
    print(json.dumps({"post_memory_convergence": summary}, ensure_ascii=False))
    return summary


def needs_memory_index(path: Path) -> bool:
    if not path.exists() or not NAS_MEMORY_DB.exists():
        return path.exists()
    try:
        with sqlite3.connect(NAS_MEMORY_DB) as conn:
            return not document_exists_for_path(conn, str(path))
    except sqlite3.Error:
        return True


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_file_state(path: Path) -> dict[str, Any]:
    try:
        stat = path.stat()
        return {
            "sha256": sha256_file(path),
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "state_error": "",
        }
    except (OSError, TimeoutError) as exc:
        return {
            "sha256": "",
            "size": 0,
            "mtime_ns": 0,
            "state_error": f"{type(exc).__name__}: {exc}",
        }


def update_targeted_memory_state(stats: dict[str, Any]) -> None:
    state = load_json(MEMORY_STATE_PATH, {})
    if not isinstance(state, dict):
        state = {}
    files = state.setdefault("files", {})
    if not isinstance(files, dict):
        files = {}
        state["files"] = files
    for item in stats.get("paths", []):
        if not isinstance(item, dict) or int(item.get("exit_code") or 0) != 0:
            continue
        path = Path(str(item.get("path") or ""))
        doc_key = str(item.get("doc_key") or "")
        if not path.exists() or not doc_key:
            continue
        try:
            rel_path = str(path.relative_to(INBOX))
        except ValueError:
            rel_path = path.name
        file_state = safe_file_state(path)
        files[rel_path] = {
            "sha256": file_state["sha256"],
            "doc_key": doc_key,
            "indexed_at": now_iso(),
            "source_path": str(path),
            "parser": path.suffix.lower().lstrip("."),
            "size": file_state["size"],
            "mtime_ns": file_state["mtime_ns"],
        }
        if file_state["state_error"]:
            files[rel_path]["state_error"] = file_state["state_error"]
    state["updated_at"] = now_iso()
    state["last_stats"] = {
        "scanned": int(stats.get("scanned") or 0),
        "indexed": int(stats.get("indexed") or 0),
        "skipped": 0,
        "failed": int(stats.get("failed") or 0),
        "quarantined": int(stats.get("quarantined") or 0),
        "mode": "targeted_paths",
    }
    state["last_targeted_paths"] = stats.get("paths", [])
    save_json(MEMORY_STATE_PATH, state)


def recent_download_paths(
    target: dict[str, Any], state: dict[str, Any], since_ts: float
) -> list[Path]:
    candidates: list[Path] = []
    primary = primary_local_path(target, state)
    if primary and primary.exists() and primary.stat().st_mtime >= since_ts - 5:
        candidates.append(primary)

    manifest = attachment_manifest(target, state)
    if manifest and manifest.exists():
        with manifest.open("r", encoding="utf-8-sig", newline="") as file:
            for row in csv.DictReader(file):
                status = str(row.get("status") or "").lower()
                if status not in {"synced", "skipped"}:
                    continue
                raw_path = str(row.get("local_path") or "")
                if not raw_path:
                    continue
                path = Path(raw_path)
                if not path.exists():
                    continue
                if path.stat().st_mtime < since_ts - 5:
                    continue
                candidates.append(path)

    seen: set[str] = set()
    unique: list[Path] = []
    for path in candidates:
        key = str(path)
        if key in seen or not needs_memory_index(path):
            continue
        seen.add(key)
        unique.append(path)
    return unique


def available_memory_paths(
    target: dict[str, Any], state: dict[str, Any], limit: int
) -> list[Path]:
    candidates: list[Path] = []
    primary = primary_local_path(target, state)
    if primary and primary.exists() and needs_memory_index(primary):
        candidates.append(primary)

    manifest = attachment_manifest(target, state)
    if manifest and manifest.exists():
        with manifest.open("r", encoding="utf-8-sig", newline="") as file:
            for row in csv.DictReader(file):
                status = str(row.get("status") or "").lower()
                if status not in {"synced", "skipped"}:
                    continue
                raw_path = str(row.get("local_path") or "")
                if not raw_path:
                    continue
                path = Path(raw_path)
                if path.exists() and needs_memory_index(path):
                    candidates.append(path)

    seen: set[str] = set()
    unique: list[Path] = []
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
        if len(unique) >= limit:
            break
    return unique


def target_memory_complete(target: dict[str, Any], state: dict[str, Any]) -> bool:
    primary = primary_local_path(target, state)
    if not primary:
        return False
    if primary.exists() and needs_memory_index(primary):
        return False

    manifest = attachment_manifest(target, state)
    if manifest:
        stats = attachment_stats(manifest)
        if int(stats.get("pending") or 0) > 0:
            return False
        if int(stats.get("available_only") or 0) > 0:
            return False
    return True


def run_memory_for_paths(paths: list[Path], env: dict[str, str]) -> dict[str, Any]:
    python = PYTHON if PYTHON.exists() else Path(sys.executable)
    stats: dict[str, Any] = {
        "scanned": len(paths),
        "indexed": 0,
        "failed": 0,
        "quarantined": 0,
        "paths": [],
    }
    for path in paths:
        result = subprocess.run(
            [
                str(python),
                str(DC_ROOT / "nas_sync" / "dc_memory_indexer.py"),
                "--path",
                str(path),
                "--no-move",
            ],
            cwd=str(DC_ROOT),
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)
        item: dict[str, Any] = {"path": str(path), "exit_code": result.returncode}
        try:
            payload = json.loads((result.stdout or "{}").strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError):
            payload = {}
        if result.returncode == 0 and int(payload.get("indexed") or 0) > 0:
            stats["indexed"] += 1
            item["doc_key"] = payload.get("doc_key") or ""
            link_path = project_folder_link(path, payload)
            if link_path is not None:
                item["project_folder_link"] = str(link_path)
        else:
            stats["failed"] += 1
            reason = payload.get("reason") or "memory_index_failed"
            item["reason"] = reason
            if should_quarantine_error(reason):
                dest = quarantine_failed_memory_path(path, reason)
                if dest is not None:
                    stats["quarantined"] += 1
                    item["quarantine_path"] = str(dest)
        stats["paths"].append(item)
    update_targeted_memory_state(stats)
    return stats


def failed_recent(
    limit: int = 12, active_failed_tokens: set[str] | None = None
) -> list[str]:
    if not FAILED.exists():
        return []
    files = [path for path in FAILED.rglob("*") if path.is_file()]
    files.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    items: list[str] = []
    active_failed_tokens = active_failed_tokens or set()
    for path in files:
        if "_cloud_space_export_failures" in path.parts:
            marker = load_json(path, {})
            token = str(marker.get("token") or "") if isinstance(marker, dict) else ""
            if token and token not in active_failed_tokens:
                continue
        items.append(str(path.relative_to(FAILED)))
        if len(items) >= limit:
            break
    return items


def build_status() -> dict[str, Any]:
    data = load_targets()
    items: list[dict[str, Any]] = []
    active_failed_tokens = {
        str(target.get("token") or target.get("admin_row_id") or "")
        for target in data.get("targets", [])
        if target.get("status") == "sync_failed_pending_review"
    }
    active_failed_tokens.discard("")
    for target in ordered_targets(data):
        token = str(target.get("token") or token_from_url(str(target.get("url") or "")))
        state = feishu_state_for(token)
        if not state and str(target.get("local_dir_hint") or "").endswith(
            "__attachments"
        ):
            state = feishu_state_by_name(
                str(target.get("local_dir_hint") or "").removesuffix("__attachments")
            )
        if not state and str(target.get("title_hint") or "").strip():
            state = feishu_state_by_name(str(target.get("title_hint") or "").strip())
        manifest = attachment_manifest(target, state)
        primary_path = primary_local_path(target, state)
        items.append(
            {
                "id": target.get("id") or token,
                "token": token,
                "url": target.get("url") or "",
                "rank_hint": target.get("rank_hint") or "",
                "size_rank": target.get("size_rank") or "",
                "size_label": target.get("size_label") or "",
                "status": target.get("status") or "active",
                "title_hint": target.get("title_hint") or state.get("name") or "",
                "feishu_done": bool(state.get("done") or primary_path),
                "local_path": str(primary_path or state.get("local_path") or ""),
                "feishu_synced_at": state.get("synced_at") or "",
                "attachments": attachment_stats(manifest),
                "memory": memory_stats(target, state),
            }
        )
    return {
        "checked_at": now_iso(),
        "source": data.get("source", {}),
        "targets": items,
        "failed_recent": failed_recent(active_failed_tokens=active_failed_tokens),
    }


def print_human(status: dict[str, Any]) -> None:
    print(f"检查时间: {status['checked_at']}")
    for item in status["targets"]:
        attachments = item["attachments"]
        memory = item["memory"]
        print("")
        print(f"- {item['id']}")
        print(f"  token: {item['token']}")
        print(
            "  大小排序: "
            f"第 {item['size_rank'] or '?'} 位; "
            f"size={item['size_label'] or '待核验'}; "
            f"{item['rank_hint'] or '无'}"
        )
        print(
            f"  飞书主文档: {'已记录' if item['feishu_done'] else '未完成'} {item['local_path']}"
        )
        print(
            "  附件: "
            f"total={attachments['total']} indexed={attachments['indexed']} "
            f"available_only={attachments['available_only']} "
            f"quarantined={attachments['quarantined']} "
            f"pending={attachments['pending']} "
            f"missing_record={attachments['missing_record']} "
            f"failed={attachments['failed']}"
        )
        print(
            "  记忆: "
            f"target_docs={memory['documents_for_target']} "
            f"total_docs={memory['total_documents']} "
            f"harness={memory['harness_nas_memories']} "
            f"open_review={memory['open_review_queue']}"
        )
        latest = memory.get("latest_indexer_stats") or {}
        if latest:
            print(f"  最近入库: {json.dumps(latest, ensure_ascii=False)}")
    if status["failed_recent"]:
        print("")
        print("最近 failed:")
        for item in status["failed_recent"]:
            print(f"  - {item}")


def run_next(args: argparse.Namespace) -> int:
    if RUN_PAUSE_PATH.exists():
        print(
            json.dumps(
                {
                    "skipped": 1,
                    "reason": "workflow_paused",
                    "pause_file": str(RUN_PAUSE_PATH),
                },
                ensure_ascii=False,
            )
        )
        return 0

    if not acquire_run_lock():
        print(
            json.dumps(
                {"skipped": 1, "reason": "workflow_already_running"}, ensure_ascii=False
            )
        )
        return 0

    data = load_targets()
    try:
        targets = [
            item
            for item in ordered_targets(data)
            if item.get("status", "queued")
            in {
                "active",
                "queued",
                "pending",
                "pending_url_verification",
                "downloaded_pending_memory_review",
            }
        ]
        if not targets:
            print(
                json.dumps(
                    {"failed": 1, "reason": "no_active_targets"}, ensure_ascii=False
                )
            )
            return 1
        target = targets[0]
        python = PYTHON if PYTHON.exists() else Path(sys.executable)
        sync_cmd = build_sync_cmd(
            target, python, args.attachment_limit, dry_run=args.dry_run
        )
        if sync_cmd is None:
            print(
                json.dumps(
                    {
                        "failed": 1,
                        "reason": "target_missing_sync_identity",
                        "target": target.get("id"),
                    },
                    ensure_ascii=False,
                )
            )
            return 1
        env = load_dotenv_env()
        sync_started_ts = time.time()
        sync_result = subprocess.run(
            sync_cmd,
            cwd=str(DC_ROOT),
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        if sync_result.stdout:
            print(sync_result.stdout, end="")
        if sync_result.stderr:
            print(sync_result.stderr, end="", file=sys.stderr)
        memory_result = None
        latest_stats: dict[str, Any] = {}
        for line in (sync_result.stdout + "\n" + sync_result.stderr).splitlines():
            if "同步统计:" not in line:
                continue
            raw = line.split("同步统计:", 1)[1].strip()
            try:
                latest_stats = ast.literal_eval(raw)
            except (SyntaxError, ValueError):
                latest_stats = {}
        has_new_downloads = any(
            int(latest_stats.get(key) or 0) > 0
            for key in ("synced", "attachment_synced")
        )
        if sync_result.returncode == 0 and has_new_downloads:
            token = str(
                target.get("token") or token_from_url(str(target.get("url") or ""))
            )
            validation_state = feishu_state_for(token)
            if not validation_state and str(target.get("title_hint") or "").strip():
                validation_state = feishu_state_by_name(
                    str(target.get("title_hint") or "").strip()
                )
            validation_error = validate_downloaded_state(target, validation_state)
            if validation_error:
                marker_path = write_failure_marker(target, validation_error)
                target["status"] = "sync_failed_pending_review"
                target["last_error"] = {
                    "ran_at": now_iso(),
                    "stage": "post_download_validation",
                    "reason": validation_error,
                    "failed_marker": marker_path,
                }
                has_new_downloads = False
                sync_result.returncode = 1
        if (
            sync_result.returncode != 0
            and not has_new_downloads
            and not str(target.get("status") or "").startswith("sync_failed")
        ):
            reason = sync_failure_summary(sync_result)
            marker_path = write_failure_marker(target, reason)
            target["status"] = "sync_failed_pending_review"
            target["last_error"] = {
                "ran_at": now_iso(),
                "stage": "feishu_sync",
                "reason": reason,
                "failed_marker": marker_path,
            }
        if args.learn and not args.dry_run:
            token = str(
                target.get("token") or token_from_url(str(target.get("url") or ""))
            )
            refreshed_state = feishu_state_for(token)
            if not refreshed_state and str(target.get("title_hint") or "").strip():
                refreshed_state = feishu_state_by_name(
                    str(target.get("title_hint") or "").strip()
                )
            paths = (
                recent_download_paths(target, refreshed_state, sync_started_ts)
                if has_new_downloads
                else []
            )
            known_paths = {str(path) for path in paths}
            for path in available_memory_paths(
                target, refreshed_state, args.attachment_limit
            ):
                if str(path) not in known_paths:
                    paths.append(path)
                    known_paths.add(str(path))
            if paths:
                memory_stats_for_run = run_memory_for_paths(paths, env)
                unquarantined_failed = max(
                    0,
                    int(memory_stats_for_run.get("failed") or 0)
                    - int(memory_stats_for_run.get("quarantined") or 0),
                )
                memory_result = subprocess.CompletedProcess(
                    args=["targeted_dc_memory"],
                    returncode=0 if unquarantined_failed == 0 else 1,
                )
                print(
                    json.dumps(
                        {"targeted_memory": memory_stats_for_run}, ensure_ascii=False
                    )
                )
                indexed = int(memory_stats_for_run.get("indexed") or 0)
                if indexed > 0:
                    convergence_result = run_post_memory_convergence(env)
                    target["last_obsidian_refs"] = {
                        "ran_at": now_iso(),
                        "exit_code": convergence_result["exit_code"],
                        "reason": "new_indexed",
                        "indexed": indexed,
                    }
                    target["last_business_convergence"] = convergence_result
                else:
                    print(
                        json.dumps(
                            {"obsidian_skipped": 1, "reason": "no_new_indexed"},
                            ensure_ascii=False,
                        )
                    )
            else:
                print(
                    json.dumps(
                        {"learn_skipped": 1, "reason": "no_memory_paths_to_index"},
                        ensure_ascii=False,
                    )
                )
            if str(target.get("status") or "").startswith("sync_failed"):
                pass
            elif target_memory_complete(target, refreshed_state):
                target["status"] = "memory_indexed_pending_review"
            elif (
                target.get("status") == "downloaded_pending_memory_review"
                or refreshed_state
            ):
                target["status"] = "downloaded_pending_memory_review"
        elif args.learn:
            print(
                json.dumps(
                    {"learn_skipped": 1, "reason": "no_new_downloads"},
                    ensure_ascii=False,
                )
            )

        target["last_sync"] = {"ran_at": now_iso(), "exit_code": sync_result.returncode}
        if memory_result is not None:
            target["last_memory"] = {
                "ran_at": now_iso(),
                "exit_code": memory_result.returncode,
            }
        save_targets(data)
        return (
            sync_result.returncode
            if sync_result.returncode
            else (memory_result.returncode if memory_result else 0)
        )
    finally:
        release_run_lock()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Feishu cloud-space NAS workflow queue/status"
    )
    parser.add_argument(
        "--status", action="store_true", help="show current queue status"
    )
    parser.add_argument("--json", action="store_true", help="print JSON status")
    parser.add_argument(
        "--run-next", action="store_true", help="run the next active cloud target"
    )
    parser.add_argument(
        "--learn", action="store_true", help="run DC memory indexer after sync"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate next sync target without writing files",
    )
    parser.add_argument(
        "--attachment-limit",
        type=int,
        default=20,
        help="max sheet attachments this run",
    )
    args = parser.parse_args()

    if args.run_next:
        return run_next(args)
    status = build_status()
    if args.json:
        print(json.dumps(status, ensure_ascii=False, indent=2))
    else:
        print_human(status)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
