from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from .contracts import (
    HARNESS_TERMINAL_STATUSES,
    HarnessArtifact,
    HarnessArtifactSpec,
    HarnessExecution,
    HarnessExecutionSettlement,
    HarnessMessageReference,
    HarnessReviewDecision,
    HarnessSessionCardPatchState,
    HarnessSessionDecision,
    HarnessSessionDecisionSource,
    HarnessTask,
    HarnessTaskCreateRequest,
    HarnessTaskEvent,
    HarnessTaskLink,
    HarnessTaskRelation,
    HarnessTaskReview,
    HarnessTaskStatus,
    HarnessWorkContext,
)


class HarnessTaskStore:
    """SQLite sidecar store for Harness task traces.

    This store is intentionally narrow:

    - task metadata lives in ``harness_tasks``
    - append-only lifecycle records live in ``harness_task_events``
    - no direct coupling to provider or tool-execution internals yet
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        self._initialized = False

    async def initialize(self) -> None:
        if self._initialized:
            return

        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA synchronous=NORMAL")
            await db.executescript("""
                CREATE TABLE IF NOT EXISTS harness_tasks (
                    task_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    platform_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    domain TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS harness_task_events (
                    event_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS harness_task_reviews (
                    review_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    reviewer_id TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    note TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS harness_work_contexts (
                    context_id TEXT PRIMARY KEY,
                    scope_key TEXT NOT NULL UNIQUE,
                    platform_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    subject_ref TEXT NOT NULL,
                    last_session_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS harness_message_refs (
                    message_ref_id TEXT PRIMARY KEY,
                    context_id TEXT NOT NULL,
                    task_id TEXT,
                    platform_message_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    content_digest TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS harness_task_links (
                    task_id TEXT PRIMARY KEY,
                    context_id TEXT NOT NULL,
                    parent_task_id TEXT,
                    relation_type TEXT NOT NULL,
                    message_ref_id TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS harness_executions (
                    execution_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    executor_kind TEXT NOT NULL,
                    capability TEXT NOT NULL,
                    status TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    request_digest TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    error_summary TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT
                );

                CREATE TABLE IF NOT EXISTS harness_artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    context_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    execution_id TEXT NOT NULL,
                    artifact_kind TEXT NOT NULL,
                    uri TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    root_artifact_id TEXT NOT NULL,
                    parent_artifact_id TEXT,
                    version INTEGER NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(root_artifact_id, version)
                );

                CREATE TABLE IF NOT EXISTS harness_execution_settlements (
                    settlement_id TEXT PRIMARY KEY,
                    execution_id TEXT NOT NULL UNIQUE,
                    task_id TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    status TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    request_digest TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    delivery_json TEXT NOT NULL,
                    artifact_id TEXT,
                    created_at TEXT NOT NULL,
                    applied_at TEXT
                );

                CREATE TABLE IF NOT EXISTS harness_session_decisions (
                    decision_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL UNIQUE,
                    unified_msg_origin TEXT NOT NULL,
                    platform_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    source_conversation_id TEXT NOT NULL,
                    target_conversation_id TEXT NOT NULL,
                    card_message_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    operator_id TEXT NOT NULL,
                    decision_source TEXT NOT NULL,
                    card_patch_state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    decided_at TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_harness_tasks_conversation
                ON harness_tasks(conversation_id, updated_at DESC);

                CREATE INDEX IF NOT EXISTS idx_harness_task_events_task
                ON harness_task_events(task_id, created_at ASC);

                CREATE INDEX IF NOT EXISTS idx_harness_task_reviews_task
                ON harness_task_reviews(task_id, created_at ASC);

                CREATE INDEX IF NOT EXISTS idx_harness_work_context_scope
                ON harness_work_contexts(scope_key);

                CREATE INDEX IF NOT EXISTS idx_harness_message_refs_context
                ON harness_message_refs(context_id, created_at DESC);

                CREATE INDEX IF NOT EXISTS idx_harness_task_links_context
                ON harness_task_links(context_id, created_at DESC);

                CREATE INDEX IF NOT EXISTS idx_harness_executions_task
                ON harness_executions(task_id, started_at DESC);

                CREATE INDEX IF NOT EXISTS idx_harness_artifacts_context
                ON harness_artifacts(context_id, artifact_kind, created_at DESC);

                CREATE INDEX IF NOT EXISTS idx_harness_settlements_status
                ON harness_execution_settlements(status, created_at ASC);

                CREATE INDEX IF NOT EXISTS idx_harness_session_decisions_session
                ON harness_session_decisions(
                    unified_msg_origin,
                    state,
                    created_at DESC
                );
            """)
            await db.commit()

        self._initialized = True

    async def create_task(
        self,
        request: HarnessTaskCreateRequest,
        *,
        task_id: str | None = None,
    ) -> HarnessTask:
        await self.initialize()

        now = self._utcnow()
        task = HarnessTask(
            task_id=task_id or uuid.uuid4().hex,
            conversation_id=request.conversation_id,
            platform_id=request.platform_id,
            session_id=request.session_id,
            title=request.title,
            domain=request.domain,
            status="pending",
            payload=request.payload,
            result={},
            created_at=now,
            updated_at=now,
        )

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO harness_tasks (
                    task_id,
                    conversation_id,
                    platform_id,
                    session_id,
                    title,
                    domain,
                    status,
                    payload_json,
                    result_json,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.task_id,
                    task.conversation_id,
                    task.platform_id,
                    task.session_id,
                    task.title,
                    task.domain,
                    task.status,
                    json.dumps(task.payload, ensure_ascii=False, sort_keys=True),
                    json.dumps(task.result, ensure_ascii=False, sort_keys=True),
                    task.created_at,
                    task.updated_at,
                ),
            )
            await db.execute(
                """
                INSERT INTO harness_task_events (
                    event_id,
                    task_id,
                    event_type,
                    payload_json,
                    created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    uuid.uuid4().hex,
                    task.task_id,
                    "task_created",
                    json.dumps(
                        {
                            "title": task.title,
                            "domain": task.domain,
                            "status": task.status,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    now,
                ),
            )
            await db.commit()

        return task

    async def get_task(self, task_id: str) -> HarnessTask | None:
        await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM harness_tasks WHERE task_id = ?",
                (task_id,),
            )
            row = await cursor.fetchone()

        if row is None:
            return None
        return self._task_from_row(row)

    async def list_tasks_for_conversation(
        self,
        conversation_id: str,
        *,
        limit: int = 20,
    ) -> list[HarnessTask]:
        await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM harness_tasks
                WHERE conversation_id = ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (conversation_id, limit),
            )
            rows = await cursor.fetchall()

        return [self._task_from_row(row) for row in rows]

    async def list_tasks_for_session(
        self,
        session_id: str,
        *,
        limit: int = 20,
        statuses: tuple[HarnessTaskStatus, ...] | None = None,
    ) -> list[HarnessTask]:
        await self.initialize()

        query = """
            SELECT * FROM harness_tasks
            WHERE session_id = ?
        """
        params: list[object] = [session_id]
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            query += f" AND status IN ({placeholders})"
            params.extend(statuses)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(query, tuple(params))
            rows = await cursor.fetchall()

        return [self._task_from_row(row) for row in rows]

    async def list_tasks(
        self,
        *,
        limit: int = 50,
        statuses: tuple[HarnessTaskStatus, ...] | None = None,
    ) -> list[HarnessTask]:
        await self.initialize()

        query = "SELECT * FROM harness_tasks"
        params: list[object] = []
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            query += f" WHERE status IN ({placeholders})"
            params.extend(statuses)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(query, tuple(params))
            rows = await cursor.fetchall()

        return [self._task_from_row(row) for row in rows]

    async def get_latest_task_for_conversation(
        self,
        conversation_id: str,
        *,
        include_terminal: bool = False,
    ) -> HarnessTask | None:
        await self.initialize()

        query = """
            SELECT * FROM harness_tasks
            WHERE conversation_id = ?
        """
        params: list[object] = [conversation_id]
        if not include_terminal:
            placeholders = ", ".join("?" for _ in HARNESS_TERMINAL_STATUSES)
            query += f" AND status NOT IN ({placeholders})"
            params.extend(sorted(HARNESS_TERMINAL_STATUSES))
        query += " ORDER BY updated_at DESC LIMIT 1"

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(query, tuple(params))
            row = await cursor.fetchone()

        if row is None:
            return None
        return self._task_from_row(row)

    async def create_session_decision(
        self,
        *,
        task_id: str,
        unified_msg_origin: str,
        platform_id: str,
        chat_id: str,
        source_conversation_id: str,
    ) -> tuple[
        HarnessSessionDecision,
        HarnessSessionDecision | None,
        bool,
    ]:
        """Create one pending session decision and supersede an older prompt.

        Args:
            task_id: Completed task or deterministic result identifier.
            unified_msg_origin: AstrBot session owning the decision.
            platform_id: AstrBot platform instance identifier.
            chat_id: Feishu destination used for card delivery.
            source_conversation_id: Conversation active at task completion.

        Returns:
            The current decision, an older decision superseded atomically when
            present, and whether a new record was inserted.
        """
        await self.initialize()

        now = self._utcnow()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT * FROM harness_session_decisions WHERE task_id = ?",
                (task_id,),
            )
            existing = await cursor.fetchone()
            if existing is not None:
                await db.commit()
                return self._session_decision_from_row(existing), None, False

            cursor = await db.execute(
                """
                SELECT * FROM harness_session_decisions
                WHERE unified_msg_origin = ? AND state = 'pending'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (unified_msg_origin,),
            )
            previous_row = await cursor.fetchone()
            previous = None
            if previous_row is not None:
                await db.execute(
                    """
                    UPDATE harness_session_decisions
                    SET state = 'superseded_continue',
                        target_conversation_id = source_conversation_id,
                        decision_source = 'superseded',
                        decided_at = ?,
                        updated_at = ?
                    WHERE decision_id = ? AND state = 'pending'
                    """,
                    (now, now, previous_row["decision_id"]),
                )
                previous_values = dict(previous_row)
                previous_values.update(
                    {
                        "state": "superseded_continue",
                        "target_conversation_id": previous_row[
                            "source_conversation_id"
                        ],
                        "decision_source": "superseded",
                        "decided_at": now,
                        "updated_at": now,
                    }
                )
                previous = self._session_decision_from_row(previous_values)

            decision = HarnessSessionDecision(
                decision_id=uuid.uuid4().hex,
                task_id=task_id,
                unified_msg_origin=unified_msg_origin,
                platform_id=platform_id,
                chat_id=chat_id,
                source_conversation_id=source_conversation_id,
                target_conversation_id="",
                card_message_id="",
                state="pending",
                operator_id="",
                decision_source="",
                card_patch_state="not_sent",
                created_at=now,
                decided_at=None,
                updated_at=now,
            )
            await db.execute(
                """
                INSERT INTO harness_session_decisions (
                    decision_id,
                    task_id,
                    unified_msg_origin,
                    platform_id,
                    chat_id,
                    source_conversation_id,
                    target_conversation_id,
                    card_message_id,
                    state,
                    operator_id,
                    decision_source,
                    card_patch_state,
                    created_at,
                    decided_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision.decision_id,
                    decision.task_id,
                    decision.unified_msg_origin,
                    decision.platform_id,
                    decision.chat_id,
                    decision.source_conversation_id,
                    decision.target_conversation_id,
                    decision.card_message_id,
                    decision.state,
                    decision.operator_id,
                    decision.decision_source,
                    decision.card_patch_state,
                    decision.created_at,
                    decision.decided_at,
                    decision.updated_at,
                ),
            )
            await db.commit()

        return decision, previous, True

    async def get_session_decision(
        self,
        decision_id: str,
    ) -> HarnessSessionDecision | None:
        """Load one durable session decision by its idempotency key.

        Args:
            decision_id: Decision identifier from the trusted card payload.

        Returns:
            The stored decision, or ``None`` when it does not exist.
        """
        await self.initialize()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM harness_session_decisions WHERE decision_id = ?",
                (decision_id,),
            )
            row = await cursor.fetchone()
        return self._session_decision_from_row(row) if row is not None else None

    async def get_pending_session_decision(
        self,
        unified_msg_origin: str,
    ) -> HarnessSessionDecision | None:
        """Load the newest actionable decision for one message session.

        Args:
            unified_msg_origin: AstrBot session identifier.

        Returns:
            The newest pending decision, or ``None``.
        """
        await self.initialize()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM harness_session_decisions
                WHERE unified_msg_origin = ? AND state = 'pending'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (unified_msg_origin,),
            )
            row = await cursor.fetchone()
        return self._session_decision_from_row(row) if row is not None else None

    async def bind_session_decision_card(
        self,
        decision_id: str,
        card_message_id: str,
    ) -> HarnessSessionDecision | None:
        """Bind the sent Feishu card to a pending decision exactly once.

        Args:
            decision_id: Durable decision identifier.
            card_message_id: Feishu message identifier returned by card send.

        Returns:
            The refreshed decision, or ``None`` when it does not exist.
        """
        await self.initialize()
        now = self._utcnow()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE harness_session_decisions
                SET card_message_id = ?, card_patch_state = 'pending', updated_at = ?
                WHERE decision_id = ? AND card_message_id = ''
                """,
                (card_message_id, now, decision_id),
            )
            await db.commit()
        return await self.get_session_decision(decision_id)

    async def continue_session_decision(
        self,
        decision_id: str,
        *,
        operator_id: str,
        decision_source: HarnessSessionDecisionSource,
    ) -> HarnessSessionDecision | None:
        """Resolve a pending decision by retaining its source conversation.

        Args:
            decision_id: Durable decision identifier.
            operator_id: User that clicked or sent the next message.
            decision_source: Explicit or implicit decision origin.

        Returns:
            The refreshed decision. Existing terminal state always wins.
        """
        await self.initialize()
        now = self._utcnow()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE harness_session_decisions
                SET state = 'continue_current',
                    target_conversation_id = source_conversation_id,
                    operator_id = ?,
                    decision_source = ?,
                    decided_at = ?,
                    updated_at = ?
                WHERE decision_id = ? AND state = 'pending'
                """,
                (operator_id, decision_source, now, now, decision_id),
            )
            await db.commit()
        return await self.get_session_decision(decision_id)

    async def begin_new_session_decision(
        self,
        decision_id: str,
        *,
        operator_id: str,
    ) -> HarnessSessionDecision | None:
        """Reserve the first valid click for new-conversation creation.

        Args:
            decision_id: Durable decision identifier.
            operator_id: User that clicked the card.

        Returns:
            The refreshed decision. Existing terminal state always wins.
        """
        await self.initialize()
        now = self._utcnow()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE harness_session_decisions
                SET state = 'opening_new',
                    operator_id = ?,
                    decision_source = 'card_click',
                    updated_at = ?
                WHERE decision_id = ? AND state = 'pending'
                """,
                (operator_id, now, decision_id),
            )
            await db.commit()
        return await self.get_session_decision(decision_id)

    async def complete_new_session_decision(
        self,
        decision_id: str,
        *,
        target_conversation_id: str,
    ) -> HarnessSessionDecision | None:
        """Persist the conversation created for an ``opening_new`` decision.

        Args:
            decision_id: Durable decision identifier.
            target_conversation_id: Newly active AstrBot conversation UUID.

        Returns:
            The refreshed decision, or ``None`` when missing.
        """
        await self.initialize()
        now = self._utcnow()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE harness_session_decisions
                SET state = 'new_conversation',
                    target_conversation_id = ?,
                    decided_at = ?,
                    updated_at = ?
                WHERE decision_id = ? AND state = 'opening_new'
                """,
                (target_conversation_id, now, now, decision_id),
            )
            await db.commit()
        return await self.get_session_decision(decision_id)

    async def mark_session_decision_card_patch(
        self,
        decision_id: str,
        patch_state: HarnessSessionCardPatchState,
    ) -> HarnessSessionDecision | None:
        """Record whether the visible Feishu card matches durable state.

        Args:
            decision_id: Durable decision identifier.
            patch_state: Current card synchronization state.

        Returns:
            The refreshed decision, or ``None`` when missing.
        """
        await self.initialize()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE harness_session_decisions
                SET card_patch_state = ?, updated_at = ?
                WHERE decision_id = ?
                """,
                (patch_state, self._utcnow(), decision_id),
            )
            await db.commit()
        return await self.get_session_decision(decision_id)

    async def get_or_create_work_context(
        self,
        *,
        scope_key: str,
        platform_id: str,
        conversation_id: str,
        subject_ref: str,
        session_id: str,
    ) -> HarnessWorkContext:
        """Return the durable work context for a stable platform scope.

        Args:
            scope_key: Stable platform, chat, subject, and domain identity.
            platform_id: Platform adapter identifier.
            conversation_id: Stable platform chat or conversation identifier.
            subject_ref: Opaque employee or sender reference.
            session_id: Current unified message origin for observability only.

        Returns:
            Existing or newly created work context.

        Raises:
            ValueError: If ``scope_key`` is empty.
        """
        await self.initialize()
        if not scope_key.strip():
            raise ValueError("scope_key must not be empty")

        now = self._utcnow()
        context_id = uuid.uuid4().hex
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute(
                """
                INSERT INTO harness_work_contexts (
                    context_id,
                    scope_key,
                    platform_id,
                    conversation_id,
                    subject_ref,
                    last_session_id,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(scope_key) DO UPDATE SET
                    last_session_id = excluded.last_session_id,
                    updated_at = excluded.updated_at
                """,
                (
                    context_id,
                    scope_key,
                    platform_id,
                    conversation_id,
                    subject_ref,
                    session_id,
                    now,
                    now,
                ),
            )
            cursor = await db.execute(
                "SELECT * FROM harness_work_contexts WHERE scope_key = ?",
                (scope_key,),
            )
            row = await cursor.fetchone()
            await db.commit()

        assert row is not None
        return self._work_context_from_row(row)

    async def record_message_reference(
        self,
        *,
        context_id: str,
        task_id: str | None,
        platform_message_id: str,
        session_id: str,
        direction: str,
        content: str,
    ) -> HarnessMessageReference:
        """Persist a message pointer and digest without copying message content.

        Args:
            context_id: Owning work context identifier.
            task_id: Optional Harness task linked to the message.
            platform_message_id: Native platform message identifier when available.
            session_id: Unified message origin observed for this message.
            direction: Message direction such as ``inbound`` or ``outbound``.
            content: Message content used only to compute a SHA-256 digest.

        Returns:
            Stored message reference.
        """
        await self.initialize()
        reference = HarnessMessageReference(
            message_ref_id=uuid.uuid4().hex,
            context_id=context_id,
            task_id=task_id,
            platform_message_id=platform_message_id,
            session_id=session_id,
            direction=direction,
            content_digest=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            created_at=self._utcnow(),
        )
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO harness_message_refs (
                    message_ref_id,
                    context_id,
                    task_id,
                    platform_message_id,
                    session_id,
                    direction,
                    content_digest,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    reference.message_ref_id,
                    reference.context_id,
                    reference.task_id,
                    reference.platform_message_id,
                    reference.session_id,
                    reference.direction,
                    reference.content_digest,
                    reference.created_at,
                ),
            )
            await db.commit()
        return reference

    async def link_task_to_context(
        self,
        *,
        task_id: str,
        context_id: str,
        relation_type: HarnessTaskRelation,
        parent_task_id: str | None = None,
        message_ref_id: str | None = None,
    ) -> HarnessTaskLink:
        """Link an immutable Harness task into a work-context task family.

        Args:
            task_id: Harness task identifier.
            context_id: Owning work context identifier.
            relation_type: ``root`` for new work or ``revision`` for follow-up work.
            parent_task_id: Completed source task for a revision.
            message_ref_id: Optional inbound message reference.

        Returns:
            Stored task link.

        Raises:
            ValueError: If the relation is invalid or a revision lacks a parent.
            RuntimeError: If a task was already linked differently.
        """
        await self.initialize()
        if relation_type not in {"root", "revision"}:
            raise ValueError(f"unsupported task relation: {relation_type!r}")
        if relation_type == "revision" and not parent_task_id:
            raise ValueError("revision task requires parent_task_id")

        link = HarnessTaskLink(
            task_id=task_id,
            context_id=context_id,
            parent_task_id=parent_task_id,
            relation_type=relation_type,
            message_ref_id=message_ref_id,
            created_at=self._utcnow(),
        )
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT 1 FROM harness_tasks WHERE task_id = ?",
                (task_id,),
            )
            if await cursor.fetchone() is None:
                raise LookupError(f"task {task_id!r} not found")
            await db.execute(
                """
                INSERT OR IGNORE INTO harness_task_links (
                    task_id,
                    context_id,
                    parent_task_id,
                    relation_type,
                    message_ref_id,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    link.task_id,
                    link.context_id,
                    link.parent_task_id,
                    link.relation_type,
                    link.message_ref_id,
                    link.created_at,
                ),
            )
            cursor = await db.execute(
                "SELECT * FROM harness_task_links WHERE task_id = ?",
                (task_id,),
            )
            row = await cursor.fetchone()
            await db.commit()

        assert row is not None
        stored = self._task_link_from_row(row)
        if (
            stored.context_id != context_id
            or stored.parent_task_id != parent_task_id
            or stored.relation_type != relation_type
        ):
            raise RuntimeError(f"task {task_id!r} is already linked differently")
        return stored

    async def get_task_link(self, task_id: str) -> HarnessTaskLink | None:
        """Return a task's work-context relationship.

        Args:
            task_id: Harness task identifier.

        Returns:
            Task link when present.
        """
        await self.initialize()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM harness_task_links WHERE task_id = ?",
                (task_id,),
            )
            row = await cursor.fetchone()
        return self._task_link_from_row(row) if row is not None else None

    async def start_execution(
        self,
        *,
        task_id: str,
        executor_kind: str,
        capability: str,
        idempotency_key: str,
        request_digest: str,
        metadata: dict | None = None,
    ) -> HarnessExecution:
        """Start or recover one idempotent executor attempt.

        Args:
            task_id: Harness task receiving the execution.
            executor_kind: Adapter family such as ``media``.
            capability: Concrete executor capability.
            idempotency_key: Stable callback and recovery key.
            request_digest: Digest of the normalized execution request.
            metadata: Non-sensitive executor metadata.

        Returns:
            Existing or newly started execution.

        Raises:
            ValueError: If the idempotency key is empty.
            LookupError: If the Harness task does not exist.
            RuntimeError: If the key already belongs to another task.
        """
        await self.initialize()
        if not idempotency_key.strip():
            raise ValueError("idempotency_key must not be empty")
        execution = HarnessExecution(
            execution_id=uuid.uuid4().hex,
            task_id=task_id,
            executor_kind=executor_kind,
            capability=capability,
            status="running",
            idempotency_key=idempotency_key,
            request_digest=request_digest,
            metadata=metadata or {},
            error_summary="",
            started_at=self._utcnow(),
            finished_at=None,
        )
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute(
                """
                INSERT OR IGNORE INTO harness_executions (
                    execution_id,
                    task_id,
                    executor_kind,
                    capability,
                    status,
                    idempotency_key,
                    request_digest,
                    metadata_json,
                    error_summary,
                    started_at,
                    finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    execution.execution_id,
                    execution.task_id,
                    execution.executor_kind,
                    execution.capability,
                    execution.status,
                    execution.idempotency_key,
                    execution.request_digest,
                    json.dumps(execution.metadata, ensure_ascii=False, sort_keys=True),
                    execution.error_summary,
                    execution.started_at,
                    execution.finished_at,
                ),
            )
            cursor = await db.execute(
                "SELECT * FROM harness_executions WHERE idempotency_key = ?",
                (idempotency_key,),
            )
            row = await cursor.fetchone()
            await db.commit()

        assert row is not None
        stored = self._execution_from_row(row)
        if stored.task_id != task_id:
            raise RuntimeError(
                f"execution key {idempotency_key!r} belongs to another task"
            )
        if (
            stored.executor_kind != executor_kind
            or stored.capability != capability
            or stored.request_digest != request_digest
        ):
            raise RuntimeError(
                f"execution key {idempotency_key!r} was replayed with a different request"
            )
        return stored

    async def get_execution(self, execution_id: str) -> HarnessExecution | None:
        """Return one executor attempt.

        Args:
            execution_id: Execution identifier.

        Returns:
            Execution when present.
        """
        await self.initialize()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM harness_executions WHERE execution_id = ?",
                (execution_id,),
            )
            row = await cursor.fetchone()
        return self._execution_from_row(row) if row is not None else None

    async def get_latest_execution_for_task(
        self, task_id: str
    ) -> HarnessExecution | None:
        """Return the latest executor attempt for a Harness task.

        Args:
            task_id: Harness task identifier.

        Returns:
            Latest execution when present.
        """
        await self.initialize()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM harness_executions WHERE task_id = ? ORDER BY started_at DESC LIMIT 1",
                (task_id,),
            )
            row = await cursor.fetchone()
        return self._execution_from_row(row) if row is not None else None

    async def settle_execution_with_artifact(
        self,
        *,
        execution_id: str,
        context_id: str,
        task_id: str,
        artifact_kind: str,
        uri: str,
        mime_type: str,
        idempotency_key: str,
        metadata: dict | None = None,
        parent_artifact_id: str | None = None,
    ) -> HarnessArtifact:
        """Atomically complete an execution and append one artifact version.

        Args:
            execution_id: Running executor attempt.
            context_id: Owning work context.
            task_id: Harness task receiving the artifact.
            artifact_kind: Artifact type such as ``image`` or ``video``.
            uri: Durable local path or remote output reference.
            mime_type: Artifact MIME type.
            idempotency_key: Stable settlement callback key.
            metadata: Non-sensitive artifact metadata.
            parent_artifact_id: Source artifact for a revision.

        Returns:
            Existing or newly stored artifact.

        Raises:
            ValueError: If required artifact identity fields are empty.
            LookupError: If the execution or parent artifact does not exist.
            RuntimeError: If execution ownership or state is inconsistent.
        """
        await self.initialize()
        if not idempotency_key.strip():
            raise ValueError("idempotency_key must not be empty")
        if not artifact_kind.strip() or not uri.strip() or not mime_type.strip():
            raise ValueError("artifact kind, uri, and MIME type must not be empty")
        now = self._utcnow()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT * FROM harness_artifacts WHERE idempotency_key = ?",
                (idempotency_key,),
            )
            replay_row = await cursor.fetchone()
            if replay_row is not None:
                replay = self._artifact_from_row(replay_row)
                if replay.execution_id != execution_id:
                    await db.rollback()
                    raise RuntimeError(
                        f"artifact key {idempotency_key!r} belongs to another execution"
                    )
                if (
                    replay.context_id != context_id
                    or replay.task_id != task_id
                    or replay.artifact_kind != artifact_kind
                    or replay.uri != uri
                    or replay.mime_type != mime_type
                    or replay.parent_artifact_id != parent_artifact_id
                ):
                    await db.rollback()
                    raise RuntimeError(
                        f"artifact key {idempotency_key!r} was replayed with a different artifact"
                    )
                await db.commit()
                return replay

            cursor = await db.execute(
                "SELECT * FROM harness_executions WHERE execution_id = ?",
                (execution_id,),
            )
            execution_row = await cursor.fetchone()
            if execution_row is None:
                await db.rollback()
                raise LookupError(f"execution {execution_id!r} not found")
            execution = self._execution_from_row(execution_row)
            if execution.task_id != task_id:
                await db.rollback()
                raise RuntimeError("execution task does not match artifact task")
            if execution.status != "running":
                await db.rollback()
                raise RuntimeError(
                    f"cannot settle execution {execution_id!r} from {execution.status!r}"
                )

            cursor = await db.execute(
                "SELECT context_id FROM harness_task_links WHERE task_id = ?",
                (task_id,),
            )
            task_link_row = await cursor.fetchone()
            if task_link_row is None:
                await db.rollback()
                raise RuntimeError("artifact task is not linked to a work context")
            if task_link_row["context_id"] != context_id:
                await db.rollback()
                raise RuntimeError("artifact context does not match the task link")

            artifact_id = uuid.uuid4().hex
            root_artifact_id = artifact_id
            version = 1
            if parent_artifact_id:
                cursor = await db.execute(
                    "SELECT * FROM harness_artifacts WHERE artifact_id = ?",
                    (parent_artifact_id,),
                )
                parent_row = await cursor.fetchone()
                if parent_row is None:
                    await db.rollback()
                    raise LookupError(
                        f"parent artifact {parent_artifact_id!r} not found"
                    )
                parent = self._artifact_from_row(parent_row)
                if (
                    parent.context_id != context_id
                    or parent.artifact_kind != artifact_kind
                ):
                    await db.rollback()
                    raise RuntimeError(
                        "parent artifact is outside the requested lineage"
                    )
                root_artifact_id = parent.root_artifact_id
                cursor = await db.execute(
                    """
                    SELECT COALESCE(MAX(version), 0) + 1
                    FROM harness_artifacts
                    WHERE root_artifact_id = ?
                    """,
                    (root_artifact_id,),
                )
                version_row = await cursor.fetchone()
                version = int(version_row[0])

            artifact = HarnessArtifact(
                artifact_id=artifact_id,
                context_id=context_id,
                task_id=task_id,
                execution_id=execution_id,
                artifact_kind=artifact_kind,
                uri=uri,
                mime_type=mime_type,
                root_artifact_id=root_artifact_id,
                parent_artifact_id=parent_artifact_id,
                version=version,
                idempotency_key=idempotency_key,
                metadata=metadata or {},
                created_at=now,
            )
            await db.execute(
                """
                INSERT INTO harness_artifacts (
                    artifact_id,
                    context_id,
                    task_id,
                    execution_id,
                    artifact_kind,
                    uri,
                    mime_type,
                    root_artifact_id,
                    parent_artifact_id,
                    version,
                    idempotency_key,
                    metadata_json,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact.artifact_id,
                    artifact.context_id,
                    artifact.task_id,
                    artifact.execution_id,
                    artifact.artifact_kind,
                    artifact.uri,
                    artifact.mime_type,
                    artifact.root_artifact_id,
                    artifact.parent_artifact_id,
                    artifact.version,
                    artifact.idempotency_key,
                    json.dumps(artifact.metadata, ensure_ascii=False, sort_keys=True),
                    artifact.created_at,
                ),
            )
            cursor = await db.execute(
                """
                UPDATE harness_executions
                SET status = 'succeeded', finished_at = ?, error_summary = ''
                WHERE execution_id = ? AND status = 'running'
                """,
                (now, execution_id),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                raise RuntimeError(
                    f"execution {execution_id!r} changed during settlement"
                )
            await db.commit()
        return artifact

    async def fail_execution(
        self,
        execution_id: str,
        *,
        reason: str,
    ) -> HarnessExecution:
        """Mark a running execution failed without deleting prior artifacts.

        Args:
            execution_id: Execution identifier.
            reason: Redacted failure summary.

        Returns:
            Updated execution.

        Raises:
            LookupError: If the execution does not exist.
            RuntimeError: If the execution is already terminal.
        """
        await self.initialize()
        now = self._utcnow()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                UPDATE harness_executions
                SET status = 'failed', error_summary = ?, finished_at = ?
                WHERE execution_id = ? AND status = 'running'
                """,
                (reason[:1000], now, execution_id),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                current = await self.get_execution(execution_id)
                if current is None:
                    raise LookupError(f"execution {execution_id!r} not found")
                raise RuntimeError(
                    f"cannot fail execution {execution_id!r} from {current.status!r}"
                )
            await db.commit()
        updated = await self.get_execution(execution_id)
        assert updated is not None
        return updated

    async def record_execution_settlement(
        self,
        *,
        execution_id: str,
        outcome: str,
        idempotency_key: str,
        request_digest: str,
        result: dict,
        delivery: dict,
        artifact: HarnessArtifactSpec | None = None,
    ) -> HarnessExecutionSettlement:
        """Atomically close an execution and persist a pending settlement.

        Args:
            execution_id: Running executor attempt.
            outcome: Requested Harness task outcome.
            idempotency_key: Stable callback settlement key.
            request_digest: Canonical digest of settlement inputs.
            result: Redacted task result and evidence.
            delivery: Minimal delivery receipt.
            artifact: Optional durable Artifact specification.

        Returns:
            Existing or newly recorded settlement.

        Raises:
            LookupError: If the execution or Artifact context is missing.
            RuntimeError: If ownership, state, or replay inputs conflict.
        """
        await self.initialize()
        now = self._utcnow()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT * FROM harness_execution_settlements WHERE idempotency_key = ?",
                (idempotency_key,),
            )
            replay_row = await cursor.fetchone()
            if replay_row is not None:
                replay = self._settlement_from_row(replay_row)
                if (
                    replay.execution_id != execution_id
                    or replay.outcome != outcome
                    or replay.request_digest != request_digest
                ):
                    await db.rollback()
                    raise RuntimeError(
                        f"settlement key {idempotency_key!r} was replayed with a different settlement"
                    )
                await db.commit()
                return replay

            cursor = await db.execute(
                "SELECT * FROM harness_execution_settlements WHERE execution_id = ?",
                (execution_id,),
            )
            if await cursor.fetchone() is not None:
                await db.rollback()
                raise RuntimeError(
                    f"execution {execution_id!r} already has a different settlement"
                )
            cursor = await db.execute(
                "SELECT * FROM harness_executions WHERE execution_id = ?",
                (execution_id,),
            )
            execution_row = await cursor.fetchone()
            if execution_row is None:
                await db.rollback()
                raise LookupError(f"execution {execution_id!r} not found")
            execution = self._execution_from_row(execution_row)
            if execution.status != "running":
                await db.rollback()
                raise RuntimeError(
                    f"cannot settle execution {execution_id!r} from {execution.status!r}"
                )

            artifact_id: str | None = None
            if artifact is not None:
                cursor = await db.execute(
                    "SELECT context_id FROM harness_task_links WHERE task_id = ?",
                    (execution.task_id,),
                )
                link_row = await cursor.fetchone()
                if link_row is None or link_row["context_id"] != artifact.context_id:
                    await db.rollback()
                    raise RuntimeError("Artifact context does not match the task link")
                artifact_id = uuid.uuid4().hex
                root_artifact_id = artifact_id
                version = 1
                if artifact.parent_artifact_id:
                    cursor = await db.execute(
                        "SELECT * FROM harness_artifacts WHERE artifact_id = ?",
                        (artifact.parent_artifact_id,),
                    )
                    parent_row = await cursor.fetchone()
                    if parent_row is None:
                        await db.rollback()
                        raise LookupError(
                            f"parent artifact {artifact.parent_artifact_id!r} not found"
                        )
                    parent = self._artifact_from_row(parent_row)
                    if (
                        parent.context_id != artifact.context_id
                        or parent.artifact_kind != artifact.artifact_kind
                    ):
                        await db.rollback()
                        raise RuntimeError(
                            "parent Artifact is outside the requested lineage"
                        )
                    root_artifact_id = parent.root_artifact_id
                    cursor = await db.execute(
                        "SELECT COALESCE(MAX(version), 0) + 1 FROM harness_artifacts WHERE root_artifact_id = ?",
                        (root_artifact_id,),
                    )
                    version = int((await cursor.fetchone())[0])
                await db.execute(
                    """
                    INSERT INTO harness_artifacts (
                        artifact_id, context_id, task_id, execution_id,
                        artifact_kind, uri, mime_type, root_artifact_id,
                        parent_artifact_id, version, idempotency_key,
                        metadata_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        artifact_id,
                        artifact.context_id,
                        execution.task_id,
                        execution_id,
                        artifact.artifact_kind,
                        artifact.uri,
                        artifact.mime_type,
                        root_artifact_id,
                        artifact.parent_artifact_id,
                        version,
                        f"artifact:{idempotency_key}",
                        json.dumps(
                            artifact.metadata, ensure_ascii=False, sort_keys=True
                        ),
                        now,
                    ),
                )

            execution_status = (
                "failed"
                if outcome == "failed"
                else "cancelled"
                if outcome == "cancelled"
                else "succeeded"
            )
            error_summary = (
                str(result.get("error") or result.get("reason") or "")[:1000]
                if execution_status == "failed"
                else ""
            )
            await db.execute(
                "UPDATE harness_executions SET status = ?, error_summary = ?, finished_at = ? WHERE execution_id = ? AND status = 'running'",
                (execution_status, error_summary, now, execution_id),
            )
            settlement = HarnessExecutionSettlement(
                settlement_id=uuid.uuid4().hex,
                execution_id=execution_id,
                task_id=execution.task_id,
                outcome=outcome,
                status="pending",
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                result=result,
                delivery=delivery,
                artifact_id=artifact_id,
                created_at=now,
                applied_at=None,
            )
            await db.execute(
                """
                INSERT INTO harness_execution_settlements (
                    settlement_id, execution_id, task_id, outcome, status,
                    idempotency_key, request_digest, result_json, delivery_json,
                    artifact_id, created_at, applied_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    settlement.settlement_id,
                    settlement.execution_id,
                    settlement.task_id,
                    settlement.outcome,
                    settlement.status,
                    settlement.idempotency_key,
                    settlement.request_digest,
                    json.dumps(result, ensure_ascii=False, sort_keys=True),
                    json.dumps(delivery, ensure_ascii=False, sort_keys=True),
                    settlement.artifact_id,
                    settlement.created_at,
                    settlement.applied_at,
                ),
            )
            await db.commit()
        return settlement

    async def list_pending_execution_settlements(
        self,
    ) -> list[HarnessExecutionSettlement]:
        """Return durable settlements whose task transition is not applied."""
        await self.initialize()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM harness_execution_settlements WHERE status = 'pending' ORDER BY created_at ASC"
            )
            rows = await cursor.fetchall()
        return [self._settlement_from_row(row) for row in rows]

    async def mark_execution_settlement_applied(
        self, settlement_id: str
    ) -> HarnessExecutionSettlement:
        """Mark one pending settlement task transition as applied."""
        await self.initialize()
        now = self._utcnow()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute(
                "UPDATE harness_execution_settlements SET status = 'applied', applied_at = ? WHERE settlement_id = ? AND status = 'pending'",
                (now, settlement_id),
            )
            await db.commit()
            cursor = await db.execute(
                "SELECT * FROM harness_execution_settlements WHERE settlement_id = ?",
                (settlement_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            raise LookupError(f"settlement {settlement_id!r} not found")
        return self._settlement_from_row(row)

    async def cancel_execution(
        self,
        execution_id: str,
        *,
        reason: str,
    ) -> HarnessExecution:
        """Cancel a running execution without deleting completed artifacts.

        Args:
            execution_id: Execution identifier.
            reason: User-visible cancellation summary.

        Returns:
            Updated execution.

        Raises:
            LookupError: If the execution does not exist.
            RuntimeError: If the execution is already terminal.
        """
        await self.initialize()
        now = self._utcnow()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                UPDATE harness_executions
                SET status = 'cancelled', error_summary = ?, finished_at = ?
                WHERE execution_id = ? AND status = 'running'
                """,
                (reason[:1000], now, execution_id),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                current = await self.get_execution(execution_id)
                if current is None:
                    raise LookupError(f"execution {execution_id!r} not found")
                raise RuntimeError(
                    f"cannot cancel execution {execution_id!r} from {current.status!r}"
                )
            await db.commit()
        updated = await self.get_execution(execution_id)
        assert updated is not None
        return updated

    async def get_artifact(self, artifact_id: str) -> HarnessArtifact | None:
        """Return one durable artifact.

        Args:
            artifact_id: Artifact identifier.

        Returns:
            Artifact when present.
        """
        await self.initialize()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM harness_artifacts WHERE artifact_id = ?",
                (artifact_id,),
            )
            row = await cursor.fetchone()
        return self._artifact_from_row(row) if row is not None else None

    async def get_latest_artifact(
        self,
        *,
        scope_key: str,
        artifact_kind: str,
    ) -> HarnessArtifact | None:
        """Resolve the newest artifact for a stable work scope.

        Args:
            scope_key: Stable work-context identity.
            artifact_kind: Required artifact kind.

        Returns:
            Latest matching artifact when present.
        """
        await self.initialize()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT artifact.*
                FROM harness_artifacts AS artifact
                JOIN harness_work_contexts AS context
                  ON context.context_id = artifact.context_id
                WHERE context.scope_key = ? AND artifact.artifact_kind = ?
                ORDER BY artifact.created_at DESC, artifact.version DESC
                LIMIT 1
                """,
                (scope_key, artifact_kind),
            )
            row = await cursor.fetchone()
        return self._artifact_from_row(row) if row is not None else None

    async def append_event(
        self,
        task_id: str,
        event_type: str,
        payload: dict,
    ) -> HarnessTaskEvent:
        await self.initialize()

        event = HarnessTaskEvent(
            event_id=uuid.uuid4().hex,
            task_id=task_id,
            event_type=event_type,
            payload=payload,
            created_at=self._utcnow(),
        )

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO harness_task_events (
                    event_id,
                    task_id,
                    event_type,
                    payload_json,
                    created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.task_id,
                    event.event_type,
                    json.dumps(event.payload, ensure_ascii=False, sort_keys=True),
                    event.created_at,
                ),
            )
            await db.commit()

        return event

    async def merge_task_payload(
        self,
        task_id: str,
        patch: dict,
        *,
        event_type: str = "payload_merged",
    ) -> HarnessTask:
        await self.initialize()

        existing = await self.get_task(task_id)
        if existing is None:
            raise LookupError(f"task {task_id!r} not found")

        now = self._utcnow()
        next_payload = {**existing.payload, **patch}

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE harness_tasks
                SET payload_json = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (
                    json.dumps(next_payload, ensure_ascii=False, sort_keys=True),
                    now,
                    task_id,
                ),
            )
            await db.execute(
                """
                INSERT INTO harness_task_events (
                    event_id,
                    task_id,
                    event_type,
                    payload_json,
                    created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    uuid.uuid4().hex,
                    task_id,
                    event_type,
                    json.dumps(patch, ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
            await db.commit()

        updated = await self.get_task(task_id)
        assert updated is not None
        return updated

    async def update_task_status(
        self,
        task_id: str,
        status: HarnessTaskStatus,
        *,
        result: dict | None = None,
        event_payload: dict | None = None,
        expected_status: HarnessTaskStatus | None = None,
    ) -> HarnessTask:
        await self.initialize()

        existing = await self.get_task(task_id)
        if existing is None:
            raise LookupError(f"task {task_id!r} not found")

        now = self._utcnow()
        next_result = result if result is not None else existing.result

        async with aiosqlite.connect(self.db_path) as db:
            if expected_status is None:
                where_clause = "WHERE task_id = ?"
                params: tuple[object, ...] = (
                    status,
                    json.dumps(next_result, ensure_ascii=False, sort_keys=True),
                    now,
                    task_id,
                )
            else:
                where_clause = "WHERE task_id = ? AND status = ?"
                params = (
                    status,
                    json.dumps(next_result, ensure_ascii=False, sort_keys=True),
                    now,
                    task_id,
                    expected_status,
                )
            cursor = await db.execute(
                f"""
                UPDATE harness_tasks
                SET status = ?, result_json = ?, updated_at = ?
                {where_clause}
                """,
                params,
            )
            if expected_status is not None and cursor.rowcount != 1:
                await db.rollback()
                current = await self.get_task(task_id)
                current_status = current.status if current is not None else "missing"
                raise RuntimeError(
                    "task status changed before update: "
                    f"{task_id!r} expected {expected_status!r}, "
                    f"current {current_status!r}"
                )
            await db.execute(
                """
                INSERT INTO harness_task_events (
                    event_id,
                    task_id,
                    event_type,
                    payload_json,
                    created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    uuid.uuid4().hex,
                    task_id,
                    "status_changed",
                    json.dumps(
                        {
                            "status": status,
                            **(event_payload or {}),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    now,
                ),
            )
            await db.commit()

        updated = await self.get_task(task_id)
        assert updated is not None
        return updated

    async def list_events(
        self,
        task_id: str,
    ) -> list[HarnessTaskEvent]:
        await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM harness_task_events
                WHERE task_id = ?
                ORDER BY created_at ASC
                """,
                (task_id,),
            )
            rows = await cursor.fetchall()

        return [
            HarnessTaskEvent(
                event_id=row["event_id"],
                task_id=row["task_id"],
                event_type=row["event_type"],
                payload=json.loads(row["payload_json"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    async def create_review(
        self,
        task_id: str,
        reviewer_id: str,
        decision: HarnessReviewDecision,
        note: str,
    ) -> HarnessTaskReview:
        await self.initialize()

        review = HarnessTaskReview(
            review_id=uuid.uuid4().hex,
            task_id=task_id,
            reviewer_id=reviewer_id,
            decision=decision,
            note=note,
            created_at=self._utcnow(),
        )

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO harness_task_reviews (
                    review_id,
                    task_id,
                    reviewer_id,
                    decision,
                    note,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    review.review_id,
                    review.task_id,
                    review.reviewer_id,
                    review.decision,
                    review.note,
                    review.created_at,
                ),
            )
            await db.commit()

        return review

    async def list_reviews(
        self,
        task_id: str,
    ) -> list[HarnessTaskReview]:
        await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM harness_task_reviews
                WHERE task_id = ?
                ORDER BY created_at ASC
                """,
                (task_id,),
            )
            rows = await cursor.fetchall()

        return [
            HarnessTaskReview(
                review_id=row["review_id"],
                task_id=row["task_id"],
                reviewer_id=row["reviewer_id"],
                decision=row["decision"],
                note=row["note"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def _task_from_row(self, row: aiosqlite.Row) -> HarnessTask:
        return HarnessTask(
            task_id=row["task_id"],
            conversation_id=row["conversation_id"],
            platform_id=row["platform_id"],
            session_id=row["session_id"],
            title=row["title"],
            domain=row["domain"],
            status=row["status"],
            payload=json.loads(row["payload_json"]),
            result=json.loads(row["result_json"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _work_context_from_row(self, row: aiosqlite.Row) -> HarnessWorkContext:
        return HarnessWorkContext(
            context_id=row["context_id"],
            scope_key=row["scope_key"],
            platform_id=row["platform_id"],
            conversation_id=row["conversation_id"],
            subject_ref=row["subject_ref"],
            last_session_id=row["last_session_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _task_link_from_row(self, row: aiosqlite.Row) -> HarnessTaskLink:
        return HarnessTaskLink(
            task_id=row["task_id"],
            context_id=row["context_id"],
            parent_task_id=row["parent_task_id"],
            relation_type=row["relation_type"],
            message_ref_id=row["message_ref_id"],
            created_at=row["created_at"],
        )

    def _execution_from_row(self, row: aiosqlite.Row) -> HarnessExecution:
        return HarnessExecution(
            execution_id=row["execution_id"],
            task_id=row["task_id"],
            executor_kind=row["executor_kind"],
            capability=row["capability"],
            status=row["status"],
            idempotency_key=row["idempotency_key"],
            request_digest=row["request_digest"],
            metadata=json.loads(row["metadata_json"]),
            error_summary=row["error_summary"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
        )

    def _artifact_from_row(self, row: aiosqlite.Row) -> HarnessArtifact:
        return HarnessArtifact(
            artifact_id=row["artifact_id"],
            context_id=row["context_id"],
            task_id=row["task_id"],
            execution_id=row["execution_id"],
            artifact_kind=row["artifact_kind"],
            uri=row["uri"],
            mime_type=row["mime_type"],
            root_artifact_id=row["root_artifact_id"],
            parent_artifact_id=row["parent_artifact_id"],
            version=row["version"],
            idempotency_key=row["idempotency_key"],
            metadata=json.loads(row["metadata_json"]),
            created_at=row["created_at"],
        )

    def _settlement_from_row(self, row: aiosqlite.Row) -> HarnessExecutionSettlement:
        return HarnessExecutionSettlement(
            settlement_id=row["settlement_id"],
            execution_id=row["execution_id"],
            task_id=row["task_id"],
            outcome=row["outcome"],
            status=row["status"],
            idempotency_key=row["idempotency_key"],
            request_digest=row["request_digest"],
            result=json.loads(row["result_json"]),
            delivery=json.loads(row["delivery_json"]),
            artifact_id=row["artifact_id"],
            created_at=row["created_at"],
            applied_at=row["applied_at"],
        )

    def _session_decision_from_row(
        self,
        row: aiosqlite.Row | dict[str, object],
    ) -> HarnessSessionDecision:
        return HarnessSessionDecision(
            decision_id=str(row["decision_id"]),
            task_id=str(row["task_id"]),
            unified_msg_origin=str(row["unified_msg_origin"]),
            platform_id=str(row["platform_id"]),
            chat_id=str(row["chat_id"]),
            source_conversation_id=str(row["source_conversation_id"]),
            target_conversation_id=str(row["target_conversation_id"]),
            card_message_id=str(row["card_message_id"]),
            state=row["state"],
            operator_id=str(row["operator_id"]),
            decision_source=row["decision_source"],
            card_patch_state=row["card_patch_state"],
            created_at=str(row["created_at"]),
            decided_at=(str(row["decided_at"]) if row["decided_at"] else None),
            updated_at=str(row["updated_at"]),
        )

    def _utcnow(self) -> str:
        return datetime.now(timezone.utc).isoformat()
