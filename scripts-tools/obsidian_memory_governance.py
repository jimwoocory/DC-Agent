#!/usr/bin/env python3
"""CLI control plane for Obsidian memory governance."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_DC_ROOT = Path("/Users/dianchi/DC-Agent")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    dc_root = Path(args.dc_root).resolve()
    _ensure_import_path(dc_root)

    if args.command == "doctor":
        payload = command_doctor(dc_root)
    elif args.command == "status":
        payload = command_status(dc_root)
    elif args.command == "export":
        payload = command_export(dc_root, limit=args.limit)
    elif args.command == "import":
        payload = command_import(dc_root, actor=args.actor)
    elif args.command == "promote":
        payload = command_promote(dc_root, actor=args.actor, dry_run=args.dry_run)
    else:
        parser.error(f"unknown command: {args.command}")
        return 2

    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if payload.get("ok", False) else int(payload.get("exit_code", 1))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dc-root", default=str(DEFAULT_DC_ROOT))
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("doctor")
    subparsers.add_parser("status")

    export_parser = subparsers.add_parser("export")
    export_parser.add_argument("--limit", type=int, default=50)

    import_parser = subparsers.add_parser("import")
    import_parser.add_argument("--actor", default="obsidian-governance-cli")

    promote_parser = subparsers.add_parser("promote")
    promote_parser.add_argument("--actor", default="obsidian-governance-cli")
    promote_parser.add_argument("--dry-run", action="store_true")
    return parser


def command_doctor(dc_root: Path) -> dict[str, Any]:
    paths = _paths(dc_root)
    checks = {
        "dc_root": dc_root.exists(),
        "nas_memory_db": paths["nas_db"].exists(),
        "obsidian_vault": paths["vault"].exists(),
        "governance_module": (
            dc_root / "dc_engines" / "dc_engines" / "memory_governance"
        ).exists(),
    }
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "paths": {key: str(value) for key, value in paths.items()},
        "exit_code": 0 if all(checks.values()) else 1,
    }


def command_status(dc_root: Path) -> dict[str, Any]:
    _ensure_import_path(dc_root)
    from dc_engines.memory_governance.store import MemoryGovernanceStore

    paths = _paths(dc_root)
    if not paths["governed_db"].exists():
        return {
            "ok": True,
            "total": 0,
            "by_status": {},
            "by_sensitivity": {},
            "paths": {key: str(value) for key, value in paths.items()},
        }
    store = MemoryGovernanceStore(paths["governed_db"])
    memories = store.list_memories(limit=100000)
    by_status: dict[str, int] = {}
    by_sensitivity: dict[str, int] = {}
    for memory in memories:
        by_status[memory.review_status] = by_status.get(memory.review_status, 0) + 1
        by_sensitivity[memory.sensitivity] = (
            by_sensitivity.get(memory.sensitivity, 0) + 1
        )
    return {
        "ok": True,
        "total": len(memories),
        "by_status": by_status,
        "by_sensitivity": by_sensitivity,
        "paths": {key: str(value) for key, value in paths.items()},
    }


def command_export(dc_root: Path, *, limit: int) -> dict[str, Any]:
    _ensure_import_path(dc_root)
    from dc_engines.memory_governance.exporter import export_memory_candidates
    from dc_engines.memory_governance.store import MemoryGovernanceStore

    paths = _paths(dc_root)
    store = MemoryGovernanceStore(paths["governed_db"])
    result = export_memory_candidates(
        nas_db_path=paths["nas_db"],
        vault_path=paths["vault"],
        store=store,
        limit=limit,
        now=_now_iso(),
    )
    return {
        "ok": True,
        "exported_count": result.exported_count,
        "skipped_count": result.skipped_count,
        "memory_ids": result.memory_ids,
        "note_paths": [str(path) for path in result.note_paths],
    }


def command_import(dc_root: Path, *, actor: str) -> dict[str, Any]:
    _ensure_import_path(dc_root)
    from dc_engines.memory_governance.importer import import_governance_notes
    from dc_engines.memory_governance.store import MemoryGovernanceStore

    paths = _paths(dc_root)
    store = MemoryGovernanceStore(paths["governed_db"])
    result = import_governance_notes(
        vault_path=paths["vault"],
        store=store,
        now=_now_iso(),
        actor=actor,
    )
    return {
        "ok": True,
        "imported_count": result.imported_count,
        "decision_count": result.decision_count,
        "audit_count": result.audit_count,
        "memory_ids": result.memory_ids,
        "note_paths": [str(path) for path in result.note_paths],
    }


def command_promote(dc_root: Path, *, actor: str, dry_run: bool) -> dict[str, Any]:
    _ensure_import_path(dc_root)
    from dc_engines.memory_governance.promoter import promote_governed_memories
    from dc_engines.memory_governance.store import MemoryGovernanceStore

    paths = _paths(dc_root)
    store = MemoryGovernanceStore(paths["governed_db"])
    result = promote_governed_memories(
        store=store,
        nas_db_path=paths["nas_db"],
        overrides_path=paths["overrides"],
        now=_now_iso(),
        actor=actor,
        dry_run=dry_run,
    )
    return {
        "ok": True,
        "dry_run": result.dry_run,
        "promoted_memory_ids": result.promoted_memory_ids,
        "skipped_memory_ids": result.skipped_memory_ids,
    }


def _paths(dc_root: Path) -> dict[str, Path]:
    return {
        "nas_db": dc_root / "data" / "nas_memory.db",
        "governed_db": dc_root / "data" / "governed_memory.db",
        "vault": dc_root / "ObsidianVault",
        "overrides": dc_root / "data" / "config" / "nas_memory_overrides.json",
    }


def _ensure_import_path(dc_root: Path) -> None:
    dc_engines_root = dc_root / "dc_engines"
    for path in (dc_root, dc_engines_root):
        path_str = str(path)
        if path_str not in sys.path:
            sys.path.insert(0, path_str)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())
