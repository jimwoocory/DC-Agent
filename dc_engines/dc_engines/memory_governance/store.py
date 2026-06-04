"""SQLite persistence for governed memories."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import GovernedMemory, ReviewDecision


class MemoryGovernanceStore:
    """Store governed memory state and append-only governance events."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS governed_memories (
                    memory_id TEXT PRIMARY KEY,
                    source_system TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    source_path TEXT DEFAULT '',
                    source_hash TEXT NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    canonical_text TEXT NOT NULL,
                    memory_kind TEXT NOT NULL,
                    review_status TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    sensitivity TEXT NOT NULL,
                    owner TEXT DEFAULT '',
                    project_id TEXT DEFAULT '',
                    tags_json TEXT NOT NULL,
                    links_json TEXT NOT NULL,
                    obsidian_note_path TEXT DEFAULT '',
                    governance_version INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    approved_at TEXT DEFAULT '',
                    approved_by TEXT DEFAULT ''
                );

                CREATE INDEX IF NOT EXISTS idx_governed_memories_source
                ON governed_memories(source_system, source_id);

                CREATE INDEX IF NOT EXISTS idx_governed_memories_review
                ON governed_memories(review_status, sensitivity, updated_at DESC);

                CREATE TABLE IF NOT EXISTS memory_decisions (
                    decision_id TEXT PRIMARY KEY,
                    memory_id TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    reviewer TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    before_json TEXT NOT NULL,
                    after_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_memory_decisions_memory
                ON memory_decisions(memory_id, created_at);

                CREATE TABLE IF NOT EXISTS memory_audit_log (
                    audit_id TEXT PRIMARY KEY,
                    memory_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_memory_audit_memory
                ON memory_audit_log(memory_id, created_at);
                """
            )

    def upsert_memory(self, memory: GovernedMemory) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO governed_memories (
                    memory_id, source_system, source_id, source_path, source_hash,
                    title, summary, canonical_text, memory_kind, review_status,
                    confidence, sensitivity, owner, project_id, tags_json, links_json,
                    obsidian_note_path, governance_version, created_at, updated_at,
                    approved_at, approved_by
                ) VALUES (
                    :memory_id, :source_system, :source_id, :source_path, :source_hash,
                    :title, :summary, :canonical_text, :memory_kind, :review_status,
                    :confidence, :sensitivity, :owner, :project_id, :tags_json, :links_json,
                    :obsidian_note_path, :governance_version, :created_at, :updated_at,
                    :approved_at, :approved_by
                )
                ON CONFLICT(memory_id) DO UPDATE SET
                    source_system = excluded.source_system,
                    source_id = excluded.source_id,
                    source_path = excluded.source_path,
                    source_hash = excluded.source_hash,
                    title = excluded.title,
                    summary = excluded.summary,
                    canonical_text = excluded.canonical_text,
                    memory_kind = excluded.memory_kind,
                    review_status = excluded.review_status,
                    confidence = excluded.confidence,
                    sensitivity = excluded.sensitivity,
                    owner = excluded.owner,
                    project_id = excluded.project_id,
                    tags_json = excluded.tags_json,
                    links_json = excluded.links_json,
                    obsidian_note_path = excluded.obsidian_note_path,
                    governance_version = excluded.governance_version,
                    updated_at = excluded.updated_at,
                    approved_at = excluded.approved_at,
                    approved_by = excluded.approved_by
                """,
                _memory_to_row(memory),
            )

    def get_memory(self, memory_id: str) -> GovernedMemory | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM governed_memories WHERE memory_id = ?",
                (memory_id,),
            ).fetchone()
        return _memory_from_row(row) if row else None

    def list_memories(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
    ) -> list[GovernedMemory]:
        if limit < 1:
            return []
        with self._connect() as conn:
            if status:
                rows = conn.execute(
                    """
                    SELECT * FROM governed_memories
                    WHERE review_status = ?
                    ORDER BY updated_at DESC, memory_id
                    LIMIT ?
                    """,
                    (status, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM governed_memories
                    ORDER BY updated_at DESC, memory_id
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        return [_memory_from_row(row) for row in rows]

    def record_decision(self, decision: ReviewDecision) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO memory_decisions (
                    decision_id, memory_id, decision, reviewer, reason,
                    before_json, after_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision.decision_id,
                    decision.memory_id,
                    decision.decision,
                    decision.reviewer,
                    decision.reason,
                    _json_dumps(decision.before),
                    _json_dumps(decision.after),
                    decision.created_at,
                ),
            )

    def list_decisions(self, memory_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM memory_decisions
                WHERE memory_id = ?
                ORDER BY created_at, decision_id
                """,
                (memory_id,),
            ).fetchall()
        return [_decision_row_to_dict(row) for row in rows]

    def append_audit(
        self,
        memory_id: str,
        action: str,
        actor: str,
        payload: dict[str, Any],
        *,
        audit_id: str | None = None,
        created_at: str | None = None,
    ) -> str:
        audit_id = audit_id or f"audit_{uuid.uuid4().hex}"
        created_at = created_at or _now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO memory_audit_log (
                    audit_id, memory_id, action, actor, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    audit_id,
                    memory_id,
                    action,
                    actor,
                    _json_dumps(payload),
                    created_at,
                ),
            )
        return audit_id

    def list_audit(self, memory_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM memory_audit_log
                WHERE memory_id = ?
                ORDER BY created_at, audit_id
                """,
                (memory_id,),
            ).fetchall()
        return [_audit_row_to_dict(row) for row in rows]

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn


def _memory_to_row(memory: GovernedMemory) -> dict[str, Any]:
    return {
        "memory_id": memory.memory_id,
        "source_system": memory.source_system,
        "source_id": memory.source_id,
        "source_path": memory.source_path,
        "source_hash": memory.source_hash,
        "title": memory.title,
        "summary": memory.summary,
        "canonical_text": memory.canonical_text,
        "memory_kind": memory.memory_kind,
        "review_status": memory.review_status,
        "confidence": memory.confidence,
        "sensitivity": memory.sensitivity,
        "owner": memory.owner,
        "project_id": memory.project_id,
        "tags_json": _json_dumps(memory.tags),
        "links_json": _json_dumps(memory.links),
        "obsidian_note_path": memory.obsidian_note_path,
        "governance_version": memory.governance_version,
        "created_at": memory.created_at,
        "updated_at": memory.updated_at,
        "approved_at": memory.approved_at,
        "approved_by": memory.approved_by,
    }


def _memory_from_row(row: sqlite3.Row) -> GovernedMemory:
    return GovernedMemory(
        memory_id=row["memory_id"],
        source_system=row["source_system"],
        source_id=row["source_id"],
        source_path=row["source_path"],
        source_hash=row["source_hash"],
        title=row["title"],
        summary=row["summary"],
        canonical_text=row["canonical_text"],
        memory_kind=row["memory_kind"],
        review_status=row["review_status"],
        confidence=float(row["confidence"]),
        sensitivity=row["sensitivity"],
        owner=row["owner"],
        project_id=row["project_id"],
        tags=_json_loads(row["tags_json"], []),
        links=_json_loads(row["links_json"], []),
        obsidian_note_path=row["obsidian_note_path"],
        governance_version=int(row["governance_version"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        approved_at=row["approved_at"],
        approved_by=row["approved_by"],
    )


def _decision_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "decision_id": row["decision_id"],
        "memory_id": row["memory_id"],
        "decision": row["decision"],
        "reviewer": row["reviewer"],
        "reason": row["reason"],
        "before": _json_loads(row["before_json"], {}),
        "after": _json_loads(row["after_json"], {}),
        "created_at": row["created_at"],
    }


def _audit_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "audit_id": row["audit_id"],
        "memory_id": row["memory_id"],
        "action": row["action"],
        "actor": row["actor"],
        "payload": _json_loads(row["payload_json"], {}),
        "created_at": row["created_at"],
    }


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _json_loads(value: str, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except json.JSONDecodeError:
        return fallback


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
