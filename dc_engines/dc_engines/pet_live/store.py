from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import PetEvent, PetIdentity, PetSourceRef, PetState, StoredPetEvent
from .reducer import apply_event


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_dumps(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _json_loads(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    loaded = json.loads(value)
    return loaded if isinstance(loaded, dict) else {}


class PetLiveStore:
    """SQLite store for live pet identity, event outbox, and state snapshots."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        self._lock = threading.Lock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS pet_identity_links (
                    pet_id TEXT PRIMARY KEY,
                    feishu_open_id TEXT NOT NULL DEFAULT '',
                    employee_id TEXT NOT NULL DEFAULT '',
                    desktop_session_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS pet_state_snapshots (
                    pet_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS pet_event_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pet_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    source_ref_json TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    state_before_json TEXT,
                    state_after_json TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_pet_event_outbox_pet_cursor
                    ON pet_event_outbox(pet_id, id);
                CREATE INDEX IF NOT EXISTS idx_pet_event_outbox_user_cursor
                    ON pet_event_outbox(user_id, id);
                """
            )
            self._migrate_identity_unique_constraints(conn)
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_pet_identity_feishu_unique
                ON pet_identity_links(feishu_open_id)
                WHERE feishu_open_id <> ''
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_pet_identity_employee_unique
                ON pet_identity_links(employee_id)
                WHERE employee_id <> ''
                """
            )

    def _migrate_identity_unique_constraints(self, conn: sqlite3.Connection) -> None:
        indexes = conn.execute("PRAGMA index_list(pet_identity_links)").fetchall()
        has_column_unique = any(
            row["origin"] == "u" and row["unique"] for row in indexes
        )
        if not has_column_unique:
            return
        conn.executescript(
            """
            ALTER TABLE pet_identity_links RENAME TO pet_identity_links_old;

            CREATE TABLE pet_identity_links (
                pet_id TEXT PRIMARY KEY,
                feishu_open_id TEXT NOT NULL DEFAULT '',
                employee_id TEXT NOT NULL DEFAULT '',
                desktop_session_id TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            INSERT INTO pet_identity_links (
                pet_id, feishu_open_id, employee_id, desktop_session_id,
                created_at, updated_at
            )
            SELECT
                pet_id, feishu_open_id, employee_id, desktop_session_id,
                created_at, updated_at
            FROM pet_identity_links_old;

            DROP TABLE pet_identity_links_old;
            """
        )

    def get_identity_by_feishu_open_id(
        self,
        feishu_open_id: str,
    ) -> PetIdentity | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM pet_identity_links WHERE feishu_open_id = ?",
                (feishu_open_id,),
            ).fetchone()
        return self._identity_from_row(row) if row is not None else None

    def get_identity_by_employee_id(
        self,
        employee_id: str,
    ) -> PetIdentity | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM pet_identity_links
                WHERE employee_id = ? AND employee_id <> ''
                """,
                (employee_id,),
            ).fetchone()
        return self._identity_from_row(row) if row is not None else None

    def create_identity(self, identity: PetIdentity) -> PetIdentity:
        now = _now()
        created = PetIdentity(
            pet_id=identity.pet_id,
            feishu_open_id=identity.feishu_open_id,
            employee_id=identity.employee_id,
            desktop_session_id=identity.desktop_session_id,
            created_at=now,
            updated_at=now,
        )
        state = PetState(
            pet_id=created.pet_id,
            user_id=created.employee_id or created.feishu_open_id,
        )
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO pet_identity_links (
                    pet_id, feishu_open_id, employee_id, desktop_session_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    created.pet_id,
                    created.feishu_open_id,
                    created.employee_id,
                    created.desktop_session_id,
                    created.created_at,
                    created.updated_at,
                ),
            )
            self._upsert_state(conn, state)
        return created

    def update_identity(self, pet_id: str, **fields: str) -> PetIdentity:
        allowed = {"feishu_open_id", "employee_id", "desktop_session_id"}
        patch = {key: value for key, value in fields.items() if key in allowed}
        if not patch:
            identity = self.get_identity_by_pet_id(pet_id)
            if identity is None:
                raise LookupError(f"pet identity {pet_id!r} not found")
            return identity
        patch["updated_at"] = _now()
        sets = ", ".join(f"{key} = :{key}" for key in patch)
        params = dict(patch, pet_id=pet_id)
        with self._lock, self._connect() as conn:
            conn.execute(
                f"UPDATE pet_identity_links SET {sets} WHERE pet_id = :pet_id",
                params,
            )
        identity = self.get_identity_by_pet_id(pet_id)
        if identity is None:
            raise LookupError(f"pet identity {pet_id!r} not found")
        return identity

    def merge_identity(self, *, source_pet_id: str, target_pet_id: str) -> PetIdentity:
        if source_pet_id == target_pet_id:
            identity = self.get_identity_by_pet_id(target_pet_id)
            if identity is None:
                raise LookupError(f"pet identity {target_pet_id!r} not found")
            return identity
        with self._lock, self._connect() as conn:
            target = conn.execute(
                "SELECT * FROM pet_identity_links WHERE pet_id = ?",
                (target_pet_id,),
            ).fetchone()
            source = conn.execute(
                "SELECT * FROM pet_identity_links WHERE pet_id = ?",
                (source_pet_id,),
            ).fetchone()
            if target is None or source is None:
                missing = target_pet_id if target is None else source_pet_id
                raise LookupError(f"pet identity {missing!r} not found")
            conn.execute(
                """
                UPDATE pet_event_outbox
                SET pet_id = ?
                WHERE pet_id = ?
                """,
                (target_pet_id, source_pet_id),
            )
            conn.execute(
                "DELETE FROM pet_state_snapshots WHERE pet_id = ?",
                (source_pet_id,),
            )
            conn.execute(
                "DELETE FROM pet_identity_links WHERE pet_id = ?",
                (source_pet_id,),
            )
            conn.execute(
                """
                UPDATE pet_identity_links
                SET updated_at = ?
                WHERE pet_id = ?
                """,
                (_now(), target_pet_id),
            )
        identity = self.get_identity_by_pet_id(target_pet_id)
        if identity is None:
            raise LookupError(f"pet identity {target_pet_id!r} not found")
        return identity

    def get_identity_by_pet_id(self, pet_id: str) -> PetIdentity | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM pet_identity_links WHERE pet_id = ?",
                (pet_id,),
            ).fetchone()
        return self._identity_from_row(row) if row is not None else None

    def get_identity_by_desktop_session_id(
        self,
        desktop_session_id: str,
    ) -> PetIdentity | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM pet_identity_links
                WHERE desktop_session_id = ?
                """,
                (desktop_session_id,),
            ).fetchone()
        return self._identity_from_row(row) if row is not None else None

    def get_pet_state(self, pet_id: str) -> PetState | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT state_json FROM pet_state_snapshots WHERE pet_id = ?",
                (pet_id,),
            ).fetchone()
        if row is None:
            return None
        return PetState.from_dict(_json_loads(row["state_json"]))

    def get_latest_pet_state(self) -> PetState | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT state_json FROM pet_state_snapshots
                ORDER BY updated_at DESC
                LIMIT 1
                """
            ).fetchone()
        if row is None:
            return None
        return PetState.from_dict(_json_loads(row["state_json"]))

    def append_event(self, event: PetEvent) -> StoredPetEvent:
        created_at = event.created_at or _now()
        with self._lock, self._connect() as conn:
            before = self._get_state_with_conn(conn, event.pet_id)
            if before is None:
                before = PetState(pet_id=event.pet_id, user_id=event.user_id)
            normalized = PetEvent(
                pet_id=event.pet_id,
                user_id=event.user_id,
                source=event.source,
                event_type=event.event_type,
                source_ref=event.source_ref,
                payload=dict(event.payload),
                created_at=created_at,
            )
            after = apply_event(before, normalized)
            cursor = conn.execute(
                """
                INSERT INTO pet_event_outbox (
                    pet_id, user_id, source, event_type, source_ref_json,
                    payload_json, state_before_json, state_after_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized.pet_id,
                    normalized.user_id,
                    normalized.source,
                    normalized.event_type,
                    _json_dumps(normalized.source_ref.to_dict()),
                    _json_dumps(normalized.payload),
                    _json_dumps(before.to_dict()),
                    _json_dumps(after.to_dict()),
                    normalized.created_at,
                ),
            )
            event_id = int(cursor.lastrowid)
            after = after.with_changes(last_event_id=event_id)
            conn.execute(
                """
                UPDATE pet_event_outbox
                SET state_after_json = ?
                WHERE id = ?
                """,
                (_json_dumps(after.to_dict()), event_id),
            )
            self._upsert_state(conn, after)

        return StoredPetEvent(
            id=event_id,
            pet_id=normalized.pet_id,
            user_id=normalized.user_id,
            source=normalized.source,
            event_type=normalized.event_type,
            source_ref=normalized.source_ref,
            payload=normalized.payload,
            created_at=normalized.created_at,
            state_before=before,
            state_after=after,
        )

    def list_events_after(
        self,
        pet_id: str,
        *,
        after_id: int = 0,
        limit: int = 100,
        created_from: str = "",
        created_before: str = "",
    ) -> list[StoredPetEvent]:
        clauses = ["pet_id = ?", "id > ?"]
        params: list[Any] = [pet_id, int(after_id)]
        if created_from:
            clauses.append("created_at >= ?")
            params.append(created_from)
        if created_before:
            clauses.append("created_at < ?")
            params.append(created_before)
        params.append(int(limit))
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM pet_event_outbox
                WHERE {" AND ".join(clauses)}
                ORDER BY id ASC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [self._event_from_row(row) for row in rows]

    def list_all_events_after(
        self,
        *,
        after_id: int = 0,
        limit: int = 100,
        created_from: str = "",
        created_before: str = "",
    ) -> list[StoredPetEvent]:
        clauses = ["id > ?"]
        params: list[Any] = [int(after_id)]
        if created_from:
            clauses.append("created_at >= ?")
            params.append(created_from)
        if created_before:
            clauses.append("created_at < ?")
            params.append(created_before)
        params.append(int(limit))
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM pet_event_outbox
                WHERE {" AND ".join(clauses)}
                ORDER BY id ASC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [self._event_from_row(row) for row in rows]

    def _get_state_with_conn(
        self,
        conn: sqlite3.Connection,
        pet_id: str,
    ) -> PetState | None:
        row = conn.execute(
            "SELECT state_json FROM pet_state_snapshots WHERE pet_id = ?",
            (pet_id,),
        ).fetchone()
        if row is None:
            return None
        return PetState.from_dict(_json_loads(row["state_json"]))

    def _upsert_state(self, conn: sqlite3.Connection, state: PetState) -> None:
        updated_at = state.updated_at or _now()
        normalized = state.with_changes(updated_at=updated_at)
        conn.execute(
            """
            INSERT INTO pet_state_snapshots (
                pet_id, user_id, state_json, updated_at
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(pet_id) DO UPDATE SET
                user_id = excluded.user_id,
                state_json = excluded.state_json,
                updated_at = excluded.updated_at
            """,
            (
                normalized.pet_id,
                normalized.user_id,
                _json_dumps(normalized.to_dict()),
                updated_at,
            ),
        )

    def _identity_from_row(self, row: sqlite3.Row) -> PetIdentity:
        return PetIdentity(
            pet_id=row["pet_id"],
            feishu_open_id=row["feishu_open_id"],
            employee_id=row["employee_id"],
            desktop_session_id=row["desktop_session_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _event_from_row(self, row: sqlite3.Row) -> StoredPetEvent:
        return StoredPetEvent(
            id=int(row["id"]),
            pet_id=row["pet_id"],
            user_id=row["user_id"],
            source=row["source"],
            event_type=row["event_type"],
            source_ref=PetSourceRef.from_dict(_json_loads(row["source_ref_json"])),
            payload=_json_loads(row["payload_json"]),
            created_at=row["created_at"],
            state_before=PetState.from_dict(_json_loads(row["state_before_json"])),
            state_after=PetState.from_dict(_json_loads(row["state_after_json"])),
        )
