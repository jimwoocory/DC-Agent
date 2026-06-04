"""SQLite 持久化层：飞书工作宠物助手。

只在这里写 SQL。handler / service 通过 PetStore 的方法操作数据。
表设计参考 codex 交接文档第 7 节（pets / pet_tasks / pet_events）。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any


def _now() -> str:
    """UTC ISO 字符串，足够第一版用。"""
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


class PetStore:
    """轻量 SQLite 封装。一个插件实例一个 PetStore。

    线程安全：handler 是 async，但同进程内 sqlite3 connection 不能跨线程，
    每次 `_connect()` 新建。外加 `_lock` 串行化写入，避免 SQLite BUSY。
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        self._init_schema()

    # ── 连接 ──────────────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS pets (
                    user_id TEXT PRIMARY KEY,
                    pet_name TEXT NOT NULL DEFAULT '小橘',
                    mood TEXT NOT NULL DEFAULT '精神不错',
                    energy INTEGER NOT NULL DEFAULT 62,
                    streak_days INTEGER NOT NULL DEFAULT 5,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS pet_tasks (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    source TEXT NOT NULL DEFAULT 'demo',
                    due_date TEXT,
                    completed_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_pet_tasks_user_status
                    ON pet_tasks(user_id, status);

                CREATE TABLE IF NOT EXISTS pet_events (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_pet_events_user_time
                    ON pet_events(user_id, created_at);
                """
            )

    # ── pets ──────────────────────────────────────────────────────────────

    def get_pet(self, user_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM pets WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            return _row_to_dict(row)

    def create_pet(self, user_id: str, **overrides: Any) -> dict[str, Any]:
        now = _now()
        fields = {
            "user_id": user_id,
            "pet_name": overrides.get("pet_name", "小橘"),
            "mood": overrides.get("mood", "精神不错"),
            "energy": int(overrides.get("energy", 62)),
            "streak_days": int(overrides.get("streak_days", 5)),
            "created_at": now,
            "updated_at": now,
        }
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO pets (user_id, pet_name, mood, energy,
                                  streak_days, created_at, updated_at)
                VALUES (:user_id, :pet_name, :mood, :energy,
                        :streak_days, :created_at, :updated_at)
                """,
                fields,
            )
        return fields

    def update_pet(self, user_id: str, **fields: Any) -> dict[str, Any] | None:
        """部分更新；只接受 pet_name / mood / energy / streak_days。"""
        allowed = {"pet_name", "mood", "energy", "streak_days"}
        patch = {k: v for k, v in fields.items() if k in allowed}
        if not patch:
            return self.get_pet(user_id)

        patch["updated_at"] = _now()
        sets = ", ".join(f"{k} = :{k}" for k in patch)
        params = dict(patch, user_id=user_id)
        with self._lock, self._connect() as conn:
            conn.execute(
                f"UPDATE pets SET {sets} WHERE user_id = :user_id",
                params,
            )
        return self.get_pet(user_id)

    # ── pet_tasks ─────────────────────────────────────────────────────────

    def list_tasks(
        self,
        user_id: str,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if status is None:
                rows = conn.execute(
                    """
                    SELECT * FROM pet_tasks
                    WHERE user_id = ?
                    ORDER BY status = 'pending' DESC, created_at ASC
                    LIMIT ?
                    """,
                    (user_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM pet_tasks
                    WHERE user_id = ? AND status = ?
                    ORDER BY created_at ASC
                    LIMIT ?
                    """,
                    (user_id, status, limit),
                ).fetchall()
            return [dict(r) for r in rows]

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM pet_tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            return _row_to_dict(row)

    def insert_task(
        self,
        user_id: str,
        title: str,
        source: str = "demo",
        due_date: str | None = None,
    ) -> dict[str, Any]:
        now = _now()
        task = {
            "id": uuid.uuid4().hex,
            "user_id": user_id,
            "title": title,
            "status": "pending",
            "source": source,
            "due_date": due_date,
            "completed_at": None,
            "created_at": now,
            "updated_at": now,
        }
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO pet_tasks (id, user_id, title, status, source,
                                       due_date, completed_at,
                                       created_at, updated_at)
                VALUES (:id, :user_id, :title, :status, :source,
                        :due_date, :completed_at,
                        :created_at, :updated_at)
                """,
                task,
            )
        return task

    def mark_task_done(self, task_id: str) -> dict[str, Any] | None:
        now = _now()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE pet_tasks
                SET status = 'completed',
                    completed_at = :now,
                    updated_at = :now
                WHERE id = :id AND status != 'completed'
                """,
                {"id": task_id, "now": now},
            )
        return self.get_task(task_id)

    def count_by_status(self, user_id: str) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT status, COUNT(*) AS n
                FROM pet_tasks
                WHERE user_id = ?
                GROUP BY status
                """,
                (user_id,),
            ).fetchall()
            return {r["status"]: r["n"] for r in rows}

    # ── pet_events ────────────────────────────────────────────────────────

    def log_event(
        self,
        user_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        record = {
            "id": uuid.uuid4().hex,
            "user_id": user_id,
            "event_type": event_type,
            "payload_json": json.dumps(payload or {}, ensure_ascii=False),
            "created_at": _now(),
        }
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO pet_events (id, user_id, event_type,
                                        payload_json, created_at)
                VALUES (:id, :user_id, :event_type,
                        :payload_json, :created_at)
                """,
                record,
            )
