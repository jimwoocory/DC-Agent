"""SQLite 员工目录 + 长期记忆存储。"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from .contracts import Employee, EmployeeMemory, MemoryKind, RelationType

_SCHEMA = """
CREATE TABLE IF NOT EXISTS employees (
    open_id TEXT PRIMARY KEY,
    platform_id TEXT DEFAULT '',
    display_name TEXT DEFAULT '',
    department TEXT DEFAULT '',
    role TEXT DEFAULT '',
    relation_type TEXT DEFAULT 'employee',
    preferred_address TEXT DEFAULT '',
    honorific_policy TEXT DEFAULT 'formal',
    personality_summary TEXT DEFAULT '',
    communication_style TEXT DEFAULT '',
    persona_evidence_count INTEGER DEFAULT 0,
    persona_updated_at TEXT DEFAULT '',
    skill_tags TEXT DEFAULT '[]',
    preferences TEXT DEFAULT '{}',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    interaction_count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS employee_memories (
    memory_id TEXT PRIMARY KEY,
    open_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    relevance REAL DEFAULT 0.5,
    created_at TEXT NOT NULL,
    FOREIGN KEY (open_id) REFERENCES employees(open_id)
);

CREATE INDEX IF NOT EXISTS idx_memories_open_id ON employee_memories(open_id);
CREATE INDEX IF NOT EXISTS idx_memories_relevance ON employee_memories(open_id, relevance DESC);

CREATE TABLE IF NOT EXISTS employee_context_injections (
    injection_id TEXT PRIMARY KEY,
    open_id TEXT NOT NULL,
    platform_id TEXT DEFAULT '',
    relation_type TEXT DEFAULT '',
    preferred_address TEXT DEFAULT '',
    honorific_policy TEXT DEFAULT '',
    memory_ids TEXT DEFAULT '[]',
    memory_kinds TEXT DEFAULT '[]',
    included_persona INTEGER DEFAULT 0,
    block_chars INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    FOREIGN KEY (open_id) REFERENCES employees(open_id)
);

CREATE INDEX IF NOT EXISTS idx_context_injections_open_id
ON employee_context_injections(open_id, created_at DESC);
"""

_EMPLOYEE_COLUMN_MIGRATIONS: dict[str, str] = {
    "relation_type": "ALTER TABLE employees ADD COLUMN relation_type TEXT DEFAULT 'employee'",
    "preferred_address": "ALTER TABLE employees ADD COLUMN preferred_address TEXT DEFAULT ''",
    "honorific_policy": "ALTER TABLE employees ADD COLUMN honorific_policy TEXT DEFAULT 'formal'",
    "personality_summary": "ALTER TABLE employees ADD COLUMN personality_summary TEXT DEFAULT ''",
    "communication_style": "ALTER TABLE employees ADD COLUMN communication_style TEXT DEFAULT ''",
    "persona_evidence_count": "ALTER TABLE employees ADD COLUMN persona_evidence_count INTEGER DEFAULT 0",
    "persona_updated_at": "ALTER TABLE employees ADD COLUMN persona_updated_at TEXT DEFAULT ''",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_employee(row) -> Employee:
    return Employee(
        open_id=row["open_id"],
        platform_id=row["platform_id"] or "",
        display_name=row["display_name"] or "",
        department=row["department"] or "",
        role=row["role"] or "",
        relation_type=row["relation_type"] or "employee",
        preferred_address=row["preferred_address"] or "",
        honorific_policy=row["honorific_policy"] or "formal",
        personality_summary=row["personality_summary"] or "",
        communication_style=row["communication_style"] or "",
        persona_evidence_count=row["persona_evidence_count"] or 0,
        persona_updated_at=row["persona_updated_at"] or "",
        skill_tags=json.loads(row["skill_tags"] or "[]"),
        preferences=json.loads(row["preferences"] or "{}"),
        first_seen_at=row["first_seen_at"] or "",
        last_seen_at=row["last_seen_at"] or "",
        interaction_count=row["interaction_count"] or 0,
    )


def _row_to_memory(row) -> EmployeeMemory:
    return EmployeeMemory(
        memory_id=row["memory_id"],
        open_id=row["open_id"],
        kind=row["kind"],  # type: ignore[arg-type]
        content=row["content"],
        relevance=row["relevance"] or 0.0,
        created_at=row["created_at"] or "",
    )


class EmployeeStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)

    async def initialize(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(_SCHEMA)
            await self._migrate_employee_columns(db)
            await db.commit()

    async def _migrate_employee_columns(self, db: aiosqlite.Connection) -> None:
        cursor = await db.execute("PRAGMA table_info(employees)")
        rows = await cursor.fetchall()
        existing = {row[1] for row in rows}
        for column, statement in _EMPLOYEE_COLUMN_MIGRATIONS.items():
            if column not in existing:
                await db.execute(statement)

    # ─────────────────────── employees ───────────────────────

    async def get_employee(self, open_id: str) -> Employee | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM employees WHERE open_id = ?", (open_id,)
            )
            row = await cur.fetchone()
            return _row_to_employee(row) if row else None

    async def get_or_create(
        self,
        open_id: str,
        *,
        platform_id: str = "",
        display_name: str = "",
    ) -> tuple[Employee, bool]:
        """返回 (employee, created)。已存在则 created=False。"""
        existing = await self.get_employee(open_id)
        if existing:
            return existing, False
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO employees
                    (open_id, platform_id, display_name, first_seen_at, last_seen_at,
                     interaction_count)
                VALUES (?, ?, ?, ?, ?, 0)
                """,
                (open_id, platform_id, display_name, now, now),
            )
            await db.commit()
        return await self.get_employee(open_id), True  # type: ignore[return-value]

    async def touch(self, open_id: str) -> None:
        """记录一次交互：interaction_count + 1，last_seen_at 刷新。"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE employees
                SET last_seen_at = ?,
                    interaction_count = interaction_count + 1
                WHERE open_id = ?
                """,
                (_now(), open_id),
            )
            await db.commit()

    async def update_profile(
        self,
        open_id: str,
        *,
        display_name: str | None = None,
        department: str | None = None,
        role: str | None = None,
        relation_type: RelationType | None = None,
        preferred_address: str | None = None,
        honorific_policy: str | None = None,
        personality_summary: str | None = None,
        communication_style: str | None = None,
        persona_evidence_count: int | None = None,
        persona_updated_at: str | None = None,
        skill_tags: list[str] | None = None,
        preferences: dict | None = None,
    ) -> Employee | None:
        sets: list[str] = []
        args: list = []
        if display_name is not None:
            sets.append("display_name = ?")
            args.append(display_name)
        if department is not None:
            sets.append("department = ?")
            args.append(department)
        if role is not None:
            sets.append("role = ?")
            args.append(role)
        if relation_type is not None:
            sets.append("relation_type = ?")
            args.append(relation_type)
        if preferred_address is not None:
            sets.append("preferred_address = ?")
            args.append(preferred_address)
        if honorific_policy is not None:
            sets.append("honorific_policy = ?")
            args.append(honorific_policy)
        if personality_summary is not None:
            sets.append("personality_summary = ?")
            args.append(personality_summary)
        if communication_style is not None:
            sets.append("communication_style = ?")
            args.append(communication_style)
        if persona_evidence_count is not None:
            sets.append("persona_evidence_count = ?")
            args.append(persona_evidence_count)
        if persona_updated_at is not None:
            sets.append("persona_updated_at = ?")
            args.append(persona_updated_at)
        if skill_tags is not None:
            sets.append("skill_tags = ?")
            args.append(json.dumps(skill_tags, ensure_ascii=False))
        if preferences is not None:
            sets.append("preferences = ?")
            args.append(json.dumps(preferences, ensure_ascii=False))
        if not sets:
            return await self.get_employee(open_id)
        args.append(open_id)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                f"UPDATE employees SET {', '.join(sets)} WHERE open_id = ?",
                args,
            )
            await db.commit()
        return await self.get_employee(open_id)

    async def list_employees(self, limit: int = 50) -> list[Employee]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM employees ORDER BY last_seen_at DESC LIMIT ?",
                (limit,),
            )
            rows = await cur.fetchall()
            return [_row_to_employee(r) for r in rows]

    # ─────────────────────── memories ───────────────────────

    async def add_memory(
        self,
        open_id: str,
        kind: MemoryKind,
        content: str,
        *,
        relevance: float = 0.5,
    ) -> EmployeeMemory:
        memory_id = uuid.uuid4().hex[:16]
        created_at = _now()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO employee_memories
                    (memory_id, open_id, kind, content, relevance, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (memory_id, open_id, kind, content, relevance, created_at),
            )
            await db.commit()
        return EmployeeMemory(
            memory_id=memory_id,
            open_id=open_id,
            kind=kind,
            content=content,
            relevance=relevance,
            created_at=created_at,
        )

    async def list_memories(
        self,
        open_id: str,
        *,
        limit: int = 10,
        min_relevance: float = 0.0,
    ) -> list[EmployeeMemory]:
        """按 relevance DESC + created_at DESC 排序取前 limit 条。"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT * FROM employee_memories
                WHERE open_id = ? AND relevance >= ?
                ORDER BY relevance DESC, created_at DESC
                LIMIT ?
                """,
                (open_id, min_relevance, limit),
            )
            rows = await cur.fetchall()
            return [_row_to_memory(r) for r in rows]

    async def delete_memory(self, memory_id: str) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "DELETE FROM employee_memories WHERE memory_id = ?", (memory_id,)
            )
            await db.commit()
            return cur.rowcount > 0

    # ─────────────────────── injection trace ───────────────────────

    async def add_context_injection(
        self,
        open_id: str,
        *,
        platform_id: str = "",
        relation_type: str = "",
        preferred_address: str = "",
        honorific_policy: str = "",
        memory_ids: list[str] | None = None,
        memory_kinds: list[str] | None = None,
        included_persona: bool = False,
        block_chars: int = 0,
    ) -> str:
        injection_id = uuid.uuid4().hex[:16]
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO employee_context_injections (
                    injection_id, open_id, platform_id, relation_type,
                    preferred_address, honorific_policy, memory_ids, memory_kinds,
                    included_persona, block_chars, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    injection_id,
                    open_id,
                    platform_id,
                    relation_type,
                    preferred_address,
                    honorific_policy,
                    json.dumps(memory_ids or [], ensure_ascii=False),
                    json.dumps(memory_kinds or [], ensure_ascii=False),
                    1 if included_persona else 0,
                    block_chars,
                    _now(),
                ),
            )
            await db.commit()
        return injection_id

    async def list_context_injections(
        self,
        open_id: str,
        *,
        limit: int = 10,
    ) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT * FROM employee_context_injections
                WHERE open_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (open_id, limit),
            )
            rows = await cur.fetchall()
        return [
            {
                "injection_id": row["injection_id"],
                "open_id": row["open_id"],
                "platform_id": row["platform_id"] or "",
                "relation_type": row["relation_type"] or "",
                "preferred_address": row["preferred_address"] or "",
                "honorific_policy": row["honorific_policy"] or "",
                "memory_ids": json.loads(row["memory_ids"] or "[]"),
                "memory_kinds": json.loads(row["memory_kinds"] or "[]"),
                "included_persona": bool(row["included_persona"]),
                "block_chars": row["block_chars"] or 0,
                "created_at": row["created_at"] or "",
            }
            for row in rows
        ]
