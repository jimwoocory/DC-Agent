"""Obsidian memory governance dashboard routes."""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from quart import request

from .route import Response, Route, RouteContext

PROJECT_ROOT = Path(__file__).resolve().parents[3]
UTC = timezone.utc


class MemoryGovernanceRoute(Route):
    """Control-plane APIs for the Obsidian-governed memory workflow."""

    def __init__(self, context: RouteContext, dc_root: Path | None = None) -> None:
        super().__init__(context)
        self.dc_root = dc_root or PROJECT_ROOT
        self.routes = {
            "/memory-governance/doctor": ("GET", self.doctor),
            "/memory-governance/status": ("GET", self.status),
            "/memory-governance/export": ("POST", self.export),
            "/memory-governance/import": ("POST", self.import_notes),
            "/memory-governance/promote": ("POST", self.promote),
            "/memory-governance/audit": ("GET", self.audit),
        }
        self.register_routes()

    async def doctor(self):
        paths = self._paths()
        checks = {
            "dc_root": self.dc_root.exists(),
            "nas_memory_db": paths["nas_db"].exists(),
            "obsidian_vault": paths["vault"].exists(),
            "governance_module": (
                self.dc_root / "dc_engines" / "dc_engines" / "memory_governance"
            ).exists(),
        }
        return (
            Response()
            .ok(
                {
                    "ok": all(checks.values()),
                    "checks": checks,
                    "paths": _stringify_paths(paths),
                }
            )
            .__dict__
        )

    async def status(self):
        paths = self._paths()
        payload: dict[str, Any] = {
            "total": 0,
            "by_status": {},
            "by_sensitivity": {},
            "recent": [],
            "paths": _stringify_paths(paths),
            "store_exists": paths["governed_db"].exists(),
        }
        if not paths["governed_db"].exists():
            return Response().ok(payload).__dict__

        with sqlite3.connect(paths["governed_db"]) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT memory_id, title, review_status, sensitivity, updated_at,
                       source_system, source_path
                FROM governed_memories
                ORDER BY updated_at DESC, memory_id
                LIMIT 20
                """
            ).fetchall()
            payload["total"] = conn.execute(
                "SELECT COUNT(*) FROM governed_memories"
            ).fetchone()[0]
            payload["by_status"] = _count_rows(
                conn,
                "SELECT review_status AS key, COUNT(*) AS count FROM governed_memories GROUP BY review_status",
            )
            payload["by_sensitivity"] = _count_rows(
                conn,
                "SELECT sensitivity AS key, COUNT(*) AS count FROM governed_memories GROUP BY sensitivity",
            )
            payload["recent"] = [_row_to_dict(row) for row in rows]
        return Response().ok(payload).__dict__

    async def export(self):
        self._ensure_import_path()
        from dc_engines.memory_governance.exporter import export_memory_candidates
        from dc_engines.memory_governance.store import MemoryGovernanceStore

        data = await request.get_json(silent=True) or {}
        limit = _bounded_int(data.get("limit"), default=50, minimum=1, maximum=500)
        paths = self._paths()
        result = export_memory_candidates(
            nas_db_path=paths["nas_db"],
            vault_path=paths["vault"],
            store=MemoryGovernanceStore(paths["governed_db"]),
            limit=limit,
            now=_now_iso(),
        )
        return (
            Response()
            .ok(
                {
                    "exported_count": result.exported_count,
                    "skipped_count": result.skipped_count,
                    "memory_ids": result.memory_ids,
                    "note_paths": [str(path) for path in result.note_paths],
                }
            )
            .__dict__
        )

    async def import_notes(self):
        self._ensure_import_path()
        from dc_engines.memory_governance.importer import import_governance_notes
        from dc_engines.memory_governance.store import MemoryGovernanceStore

        data = await request.get_json(silent=True) or {}
        actor = str(data.get("actor") or "dashboard-memory-governance").strip()
        paths = self._paths()
        result = import_governance_notes(
            vault_path=paths["vault"],
            store=MemoryGovernanceStore(paths["governed_db"]),
            now=_now_iso(),
            actor=actor,
        )
        return (
            Response()
            .ok(
                {
                    "imported_count": result.imported_count,
                    "decision_count": result.decision_count,
                    "audit_count": result.audit_count,
                    "memory_ids": result.memory_ids,
                    "note_paths": [str(path) for path in result.note_paths],
                }
            )
            .__dict__
        )

    async def promote(self):
        self._ensure_import_path()
        from dc_engines.memory_governance.promoter import promote_governed_memories
        from dc_engines.memory_governance.store import MemoryGovernanceStore

        data = await request.get_json(silent=True) or {}
        dry_run = bool(data.get("dry_run", True))
        actor = str(data.get("actor") or "dashboard-memory-governance").strip()
        paths = self._paths()
        result = promote_governed_memories(
            store=MemoryGovernanceStore(paths["governed_db"]),
            nas_db_path=paths["nas_db"],
            overrides_path=paths["overrides"],
            now=_now_iso(),
            actor=actor,
            dry_run=dry_run,
        )
        return (
            Response()
            .ok(
                {
                    "dry_run": result.dry_run,
                    "promoted_memory_ids": result.promoted_memory_ids,
                    "skipped_memory_ids": result.skipped_memory_ids,
                }
            )
            .__dict__
        )

    async def audit(self):
        paths = self._paths()
        if not paths["governed_db"].exists():
            return Response().ok({"items": [], "store_exists": False}).__dict__

        memory_id = str(request.args.get("memory_id") or "").strip()
        limit = _bounded_int(
            request.args.get("limit"), default=50, minimum=1, maximum=200
        )
        sql = """
            SELECT audit_id, memory_id, action, actor, payload_json, created_at
            FROM memory_audit_log
        """
        params: tuple[Any, ...]
        if memory_id:
            sql += " WHERE memory_id = ?"
            params = (memory_id, limit)
        else:
            params = (limit,)
        sql += " ORDER BY created_at DESC, audit_id DESC LIMIT ?"

        with sqlite3.connect(paths["governed_db"]) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()
        return (
            Response()
            .ok({"items": [_row_to_dict(row) for row in rows], "store_exists": True})
            .__dict__
        )

    def _paths(self) -> dict[str, Path]:
        return {
            "nas_db": self.dc_root / "data" / "nas_memory.db",
            "governed_db": self.dc_root / "data" / "governed_memory.db",
            "vault": self.dc_root / "ObsidianVault",
            "overrides": self.dc_root / "data" / "config" / "nas_memory_overrides.json",
        }

    def _ensure_import_path(self) -> None:
        for path in (
            self.dc_root,
            self.dc_root / "dc_engines",
            PROJECT_ROOT,
            PROJECT_ROOT / "dc_engines",
        ):
            path_str = str(path)
            if path_str not in sys.path:
                sys.path.insert(0, path_str)


def _count_rows(conn: sqlite3.Connection, sql: str) -> dict[str, int]:
    return {str(row["key"]): int(row["count"]) for row in conn.execute(sql).fetchall()}


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _stringify_paths(paths: dict[str, Path]) -> dict[str, str]:
    return {key: str(value) for key, value in paths.items()}


def _bounded_int(
    value: Any,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return min(max(parsed, minimum), maximum)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
