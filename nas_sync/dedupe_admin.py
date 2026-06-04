#!/usr/bin/env python3
"""NAS duplicate audit and safe quarantine utilities.

The NAS is the source of truth. This script only treats files as duplicates when
their full SHA-256 hash matches. The default mode is read-only; ``--apply`` moves
duplicate files into ``archive/dedupe_quarantine`` instead of deleting them.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"
REPORT_PATH = PROJECT_ROOT / "data" / "watchdog" / "nas_dedupe_report.json"
KB_DB_PATH = PROJECT_ROOT / "data" / "knowledge_base" / "kb.db"
INGEST_STATE_PATH = PROJECT_ROOT / "nas_sync" / "state.json"

SYSTEM_DIRS = {
    "#recycle",
    "archive",
    ".benchmark",
    ".accelerate",
    ".Spotlight-V100",
    ".TemporaryItems",
    ".Trashes",
}
SYSTEM_FILES = {".DS_Store", "desktop.ini", "Thumbs.db"}


@dataclass(slots=True)
class FileEntry:
    path: Path
    rel: str
    size: int
    mtime: float
    sha256: str = ""


def load_config() -> dict[str, Any]:
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}


def mount_point(cfg: dict[str, Any]) -> Path:
    return Path(cfg.get("nas", {}).get("mount_point", "/Users/dianchi/nas_kb"))


def resolve_scan_root(cfg: dict[str, Any], subdir: str | None) -> Path:
    root = mount_point(cfg)
    if not subdir:
        return root
    target = (root / subdir).resolve()
    if root.resolve() not in target.parents and target != root.resolve():
        raise ValueError(f"subdir escapes NAS root: {subdir}")
    return target


def iter_files(root: Path, *, include_recycle: bool = False) -> list[FileEntry]:
    entries: list[FileEntry] = []
    for path in root.rglob("*"):
        rel_parts = path.relative_to(root).parts
        if not include_recycle and any(part in SYSTEM_DIRS for part in rel_parts):
            continue
        if path.name in SYSTEM_FILES:
            continue
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        entries.append(
            FileEntry(
                path=path,
                rel=str(path.relative_to(root)),
                size=int(stat.st_size),
                mtime=float(stat.st_mtime),
            )
        )
    return entries


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def quick_fingerprint(path: Path, size: int) -> str:
    """Hash the size plus head/tail chunks before expensive full hashing."""
    digest = hashlib.sha256()
    digest.update(str(size).encode("ascii"))
    with path.open("rb") as file:
        digest.update(file.read(1024 * 1024))
        if size > 2 * 1024 * 1024:
            file.seek(max(size - 1024 * 1024, 0))
            digest.update(file.read(1024 * 1024))
    return digest.hexdigest()


def score_keep(entry: FileEntry) -> tuple[int, int, float, str]:
    rel_parts = Path(entry.rel).parts
    penalty = 0
    if "archive" in rel_parts:
        penalty += 40
    if "processed" in rel_parts:
        penalty += 30
    if "inbox" in rel_parts:
        penalty += 20
    if "百度网盘同步" in rel_parts:
        penalty += 15
    if "备份" in entry.rel:
        penalty += 8
    if "副本" in entry.rel or "copy" in entry.rel.lower():
        penalty += 5
    return (penalty, len(rel_parts), entry.mtime, entry.rel)


def build_duplicate_report(
    root: Path,
    *,
    include_recycle: bool = False,
    max_hash_size_mb: int = 64,
) -> dict[str, Any]:
    entries = iter_files(root, include_recycle=include_recycle)
    max_hash_size = max_hash_size_mb * 1024 * 1024
    by_size: dict[int, list[FileEntry]] = {}
    for entry in entries:
        by_size.setdefault(entry.size, []).append(entry)

    quick_groups: dict[tuple[int, str], list[FileEntry]] = {}
    large_candidates = []
    for group in by_size.values():
        if len(group) < 2:
            continue
        large = [entry for entry in group if entry.size > max_hash_size]
        if len(large) > 1:
            by_name: dict[str, list[FileEntry]] = {}
            for entry in large:
                by_name.setdefault(entry.path.name.lower(), []).append(entry)
            for name_group in by_name.values():
                if len(name_group) > 1:
                    ordered = sorted(name_group, key=score_keep)
                    large_candidates.append(
                        {
                            "size": ordered[0].size,
                            "keep_candidate": ordered[0].rel,
                            "duplicate_candidates": [item.rel for item in ordered[1:]],
                        }
                    )
        for entry in group:
            if entry.size > max_hash_size:
                continue
            try:
                quick = quick_fingerprint(entry.path, entry.size)
            except OSError:
                continue
            quick_groups.setdefault((entry.size, quick), []).append(entry)

    hash_candidates = [group for group in quick_groups.values() if len(group) > 1]
    by_hash: dict[str, list[FileEntry]] = {}
    for group in hash_candidates:
        for entry in group:
            try:
                entry.sha256 = sha256_file(entry.path)
            except OSError:
                continue
            by_hash.setdefault(entry.sha256, []).append(entry)

    duplicate_groups = []
    duplicate_count = 0
    duplicate_bytes = 0
    for digest, group in sorted(by_hash.items(), key=lambda item: -len(item[1])):
        if len(group) < 2:
            continue
        ordered = sorted(group, key=score_keep)
        keep = ordered[0]
        duplicates = ordered[1:]
        duplicate_count += len(duplicates)
        duplicate_bytes += sum(item.size for item in duplicates)
        duplicate_groups.append(
            {
                "sha256": digest,
                "size": keep.size,
                "keep": keep.rel,
                "duplicates": [item.rel for item in duplicates],
            }
        )

    return {
        "root": str(root),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "file_count": len(entries),
        "max_hash_size_mb": max_hash_size_mb,
        "duplicate_group_count": len(duplicate_groups),
        "duplicate_file_count": duplicate_count,
        "duplicate_bytes": duplicate_bytes,
        "large_candidate_group_count": len(large_candidates),
        "large_candidates": large_candidates,
        "groups": duplicate_groups,
    }


def write_report(report: dict[str, Any]) -> None:
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_report() -> dict[str, Any]:
    if not REPORT_PATH.exists():
        raise FileNotFoundError(f"report not found: {REPORT_PATH}")
    return json.loads(REPORT_PATH.read_text(encoding="utf-8"))


def quarantine_duplicates(report: dict[str, Any], *, apply: bool) -> dict[str, Any]:
    root = Path(report["root"])
    stamp = time.strftime("%Y%m%d_%H%M%S")
    quarantine_root = root / "archive" / "dedupe_quarantine" / stamp
    moved: list[dict[str, str]] = []
    missing: list[str] = []

    for group in report.get("groups", []):
        for rel in group.get("duplicates", []):
            source = root / rel
            if not source.exists():
                missing.append(rel)
                continue
            target = quarantine_root / rel
            moved.append({"from": rel, "to": str(target.relative_to(root))})
            if apply:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(target))

    if apply:
        manifest = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "source_report": str(REPORT_PATH),
            "moved": moved,
            "missing": missing,
        }
        quarantine_root.mkdir(parents=True, exist_ok=True)
        (quarantine_root / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    return {
        "apply": apply,
        "quarantine_root": str(quarantine_root),
        "move_count": len(moved),
        "missing_count": len(missing),
        "missing": missing,
    }


def kb_duplicates() -> dict[str, Any]:
    if not KB_DB_PATH.exists():
        return {"kb_document_count": 0, "duplicate_groups": []}

    con = sqlite3.connect(KB_DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        docs = [
            dict(row)
            for row in con.execute(
                """
                select d.doc_id, d.kb_id, d.doc_name, d.file_size, b.kb_name
                from kb_documents d
                left join knowledge_bases b on b.kb_id = d.kb_id
                order by d.created_at asc
                """
            )
        ]
    finally:
        con.close()

    grouped: dict[tuple[str, int, str], list[dict[str, Any]]] = {}
    for doc in docs:
        key = (str(doc["kb_id"]), int(doc["file_size"]), str(doc["doc_name"]))
        grouped.setdefault(key, []).append(doc)

    duplicate_groups = []
    for group in grouped.values():
        if len(group) > 1:
            duplicate_groups.append({"keep": group[0], "duplicates": group[1:]})

    return {
        "kb_document_count": len(docs),
        "duplicate_group_count": len(duplicate_groups),
        "duplicate_groups": duplicate_groups,
    }


def kb_reconcile_report() -> dict[str, Any]:
    state_doc_ids: set[str] = set()
    state_names: dict[str, str] = {}
    if INGEST_STATE_PATH.exists():
        raw = json.loads(INGEST_STATE_PATH.read_text(encoding="utf-8"))
        for rel, item in (raw.get("ingested") or {}).items():
            if isinstance(item, dict) and item.get("doc_id"):
                doc_id = str(item["doc_id"])
                state_doc_ids.add(doc_id)
                state_names[doc_id] = rel

    if not KB_DB_PATH.exists():
        return {
            "state_doc_count": len(state_doc_ids),
            "kb_doc_count": 0,
            "missing_in_kb": sorted(state_doc_ids),
            "stale_in_kb": [],
        }

    con = sqlite3.connect(KB_DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        rows = [
            dict(row)
            for row in con.execute(
                """
                select d.doc_id, d.kb_id, d.doc_name, d.file_size, b.kb_name
                from kb_documents d
                left join knowledge_bases b on b.kb_id = d.kb_id
                where b.kb_name = 'nas_knowledge'
                order by d.created_at asc
                """
            )
        ]
    finally:
        con.close()

    kb_doc_ids = {str(row["doc_id"]) for row in rows}
    stale = [row for row in rows if str(row["doc_id"]) not in state_doc_ids]
    missing = [
        {"doc_id": doc_id, "state_name": state_names.get(doc_id, "")}
        for doc_id in sorted(state_doc_ids - kb_doc_ids)
    ]
    return {
        "state_doc_count": len(state_doc_ids),
        "kb_doc_count": len(rows),
        "missing_in_kb": missing,
        "stale_in_kb": stale,
        "in_sync": not missing and not stale,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit and quarantine exact NAS duplicates"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    audit = sub.add_parser("audit", help="scan NAS and write duplicate report")
    audit.add_argument(
        "--include-recycle", action="store_true", help="include #recycle folders"
    )
    audit.add_argument(
        "--subdir",
        help="scan only one NAS subdir first, for example processed or 柳汽",
    )
    audit.add_argument(
        "--max-hash-size-mb",
        type=int,
        default=64,
        help="full-hash files up to this size; larger same-name files become candidates",
    )

    quarantine = sub.add_parser("quarantine", help="move duplicate files to quarantine")
    quarantine.add_argument("--apply", action="store_true", help="actually move files")

    sub.add_parser("kb-audit", help="show duplicate documents in AstrBot KB database")
    sub.add_parser(
        "kb-reconcile", help="compare NAS ingest state with AstrBot nas_knowledge"
    )

    args = parser.parse_args()
    cfg = load_config()
    root = resolve_scan_root(cfg, getattr(args, "subdir", None))

    if args.cmd == "audit":
        report = build_duplicate_report(
            root,
            include_recycle=args.include_recycle,
            max_hash_size_mb=args.max_hash_size_mb,
        )
        write_report(report)
        print(
            json.dumps(
                {
                    "report": str(REPORT_PATH),
                    "file_count": report["file_count"],
                    "duplicate_group_count": report["duplicate_group_count"],
                    "duplicate_file_count": report["duplicate_file_count"],
                    "duplicate_bytes": report["duplicate_bytes"],
                    "large_candidate_group_count": report[
                        "large_candidate_group_count"
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if args.cmd == "quarantine":
        result = quarantine_duplicates(load_report(), apply=args.apply)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "kb-audit":
        print(json.dumps(kb_duplicates(), ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "kb-reconcile":
        print(json.dumps(kb_reconcile_report(), ensure_ascii=False, indent=2))
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
