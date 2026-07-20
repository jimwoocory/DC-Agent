from __future__ import annotations

import json
import sqlite3
import uuid
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

import aiosqlite
import yaml


class EmployeeInsightSessionStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    OPENED = "opened"
    ENGAGED = "engaged"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    MUTED = "muted"
    FAILED = "failed"
    BLOCKED = "blocked"


class TaskStatus(StrEnum):
    CREATED = "created"
    COLLECTING_INPUTS = "collecting_inputs"
    RUNNING = "running"
    RESULT_DELIVERED = "result_delivered"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    ABANDONED = "abandoned"


class CandidateType(StrEnum):
    NEED = "need_candidate"
    TEMPLATE = "template_candidate"
    SKILL = "skill_candidate"
    ROUTER = "router_candidate"
    KNOWLEDGE_GAP = "knowledge_gap_candidate"
    ONBOARDING = "onboarding_candidate"
    HERMES_DEEP_DIVE = "hermes_deep_dive_candidate"


class ReviewStatus(StrEnum):
    REVIEW_REQUIRED = "review_required"
    APPROVED = "approved"
    REJECTED = "rejected"
    MERGED = "merged"
    STALE = "stale"
    SENSITIVE_BLOCKED = "sensitive_blocked"
    RELEASED = "released"
    VALIDATED = "validated"


class PilotStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    EXITED = "exited"


@dataclass(slots=True)
class TextSendResult:
    success: bool
    provider_message_id: str = ""
    error: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


class EmployeeInsightTextSender(Protocol):
    async def send_text(self, employee_id: str, text: str) -> TextSendResult:
        """Send one text message to an employee id."""


SESSION_TRANSITIONS: dict[
    EmployeeInsightSessionStatus, set[EmployeeInsightSessionStatus]
] = {
    EmployeeInsightSessionStatus.PENDING: {
        EmployeeInsightSessionStatus.SENT,
        EmployeeInsightSessionStatus.SKIPPED,
        EmployeeInsightSessionStatus.MUTED,
        EmployeeInsightSessionStatus.FAILED,
    },
    EmployeeInsightSessionStatus.SENT: {
        EmployeeInsightSessionStatus.OPENED,
        EmployeeInsightSessionStatus.SKIPPED,
        EmployeeInsightSessionStatus.MUTED,
        EmployeeInsightSessionStatus.FAILED,
    },
    EmployeeInsightSessionStatus.OPENED: {
        EmployeeInsightSessionStatus.ENGAGED,
        EmployeeInsightSessionStatus.SKIPPED,
        EmployeeInsightSessionStatus.MUTED,
        EmployeeInsightSessionStatus.BLOCKED,
    },
    EmployeeInsightSessionStatus.ENGAGED: {
        EmployeeInsightSessionStatus.COMPLETED,
        EmployeeInsightSessionStatus.BLOCKED,
        EmployeeInsightSessionStatus.MUTED,
    },
    EmployeeInsightSessionStatus.BLOCKED: {
        EmployeeInsightSessionStatus.ENGAGED,
        EmployeeInsightSessionStatus.MUTED,
    },
    EmployeeInsightSessionStatus.COMPLETED: set(),
    EmployeeInsightSessionStatus.SKIPPED: set(),
    EmployeeInsightSessionStatus.MUTED: set(),
    EmployeeInsightSessionStatus.FAILED: set(),
}

TASK_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.CREATED: {
        TaskStatus.COLLECTING_INPUTS,
        TaskStatus.RUNNING,
        TaskStatus.BLOCKED,
        TaskStatus.ABANDONED,
    },
    TaskStatus.COLLECTING_INPUTS: {
        TaskStatus.RUNNING,
        TaskStatus.BLOCKED,
        TaskStatus.ABANDONED,
    },
    TaskStatus.RUNNING: {
        TaskStatus.RESULT_DELIVERED,
        TaskStatus.BLOCKED,
        TaskStatus.ABANDONED,
    },
    TaskStatus.RESULT_DELIVERED: {
        TaskStatus.COMPLETED,
        TaskStatus.RUNNING,
        TaskStatus.BLOCKED,
    },
    TaskStatus.BLOCKED: {
        TaskStatus.COLLECTING_INPUTS,
        TaskStatus.RUNNING,
        TaskStatus.ABANDONED,
    },
    TaskStatus.COMPLETED: set(),
    TaskStatus.ABANDONED: set(),
}

RUNTIME_ELIGIBLE_STATUSES = {
    ReviewStatus.APPROVED,
    ReviewStatus.RELEASED,
    ReviewStatus.VALIDATED,
}

RUNTIME_BLOCKED_SENSITIVITY = {"secret", "sensitive_blocked"}


@dataclass(slots=True)
class EmployeeInsightProfile:
    employee_id: str
    employee_hash: str
    display_name: str = ""
    department_id: str = ""
    role: str = ""
    pilot_status: PilotStatus = PilotStatus.ACTIVE
    preferred_touch_time: str = ""
    last_outreach_at: str = ""
    unanswered_outreach_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: _utcnow())
    updated_at: str = field(default_factory=lambda: _utcnow())

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["pilot_status"] = self.pilot_status.value
        return data


@dataclass(slots=True)
class EmployeeInsightSession:
    session_id: str
    employee_id: str
    channel: str
    trigger_type: str
    status: EmployeeInsightSessionStatus = EmployeeInsightSessionStatus.PENDING
    task_status: TaskStatus = TaskStatus.CREATED
    department_id: str = ""
    role: str = ""
    scenario_id: str = ""
    original_request: str = ""
    normalized_goal: str = ""
    required_inputs: list[str] = field(default_factory=list)
    satisfaction: str = ""
    friction_points: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    summary: str = ""
    created_at: str = field(default_factory=lambda: _utcnow())
    updated_at: str = field(default_factory=lambda: _utcnow())

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        data["task_status"] = self.task_status.value
        return data


@dataclass(slots=True)
class InsightEvent:
    event_id: str
    session_id: str
    event_type: str
    actor: str
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: _utcnow())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class EmployeeInsightCandidate:
    candidate_id: str
    candidate_type: CandidateType
    source_session_ids: list[str]
    department_id: str
    scenario_id: str
    title: str
    summary: str
    evidence: list[dict[str, Any]]
    confidence: float
    review_status: ReviewStatus = ReviewStatus.REVIEW_REQUIRED
    sensitivity: str = "internal"
    obsidian_note_path: str = ""
    recommended_action: str = ""
    impact_scope: str = "pilot"
    created_at: str = field(default_factory=lambda: _utcnow())
    updated_at: str = field(default_factory=lambda: _utcnow())

    @property
    def is_runtime_eligible(self) -> bool:
        if self.sensitivity in RUNTIME_BLOCKED_SENSITIVITY:
            return False
        return self.review_status in RUNTIME_ELIGIBLE_STATUSES

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["candidate_type"] = self.candidate_type.value
        data["review_status"] = self.review_status.value
        data["is_runtime_eligible"] = self.is_runtime_eligible
        return data


@dataclass(slots=True)
class AuditEvent:
    audit_id: str
    action: str
    actor: str
    target_id: str
    detail: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: _utcnow())


def transition_session(
    session: EmployeeInsightSession,
    next_status: EmployeeInsightSessionStatus,
) -> EmployeeInsightSession:
    allowed = SESSION_TRANSITIONS[session.status]
    if next_status not in allowed:
        raise ValueError(
            f"illegal session transition: {session.status.value} -> {next_status.value}"
        )
    return replace(session, status=next_status, updated_at=_utcnow())


def transition_task(current: TaskStatus, next_status: TaskStatus) -> TaskStatus:
    allowed = TASK_TRANSITIONS[current]
    if next_status not in allowed:
        raise ValueError(
            f"illegal task transition: {current.value} -> {next_status.value}"
        )
    return next_status


def build_candidate_from_session(
    session: EmployeeInsightSession,
    events: list[InsightEvent],
) -> EmployeeInsightCandidate:
    candidate_type = _candidate_type_for_session(session)
    evidence = [
        {
            "event_id": event.event_id,
            "event_type": event.event_type,
            "actor": event.actor,
            "payload": event.payload,
            "created_at": event.created_at,
        }
        for event in events
    ]
    friction_text = "、".join(session.friction_points) or "未分类卡点"
    scenario = session.scenario_id or "unknown"
    title = _candidate_title(candidate_type, scenario)
    summary = (
        f"员工在 {scenario} 场景中遇到卡点：{friction_text}。"
        f"目标：{session.normalized_goal or session.original_request or '未归一化'}。"
    )
    return EmployeeInsightCandidate(
        candidate_id=(
            "empins_"
            f"{uuid.uuid5(uuid.NAMESPACE_URL, f'dc-agent:employee-insight:{session.session_id}').hex}"
        ),
        candidate_type=candidate_type,
        source_session_ids=[session.session_id],
        department_id=session.department_id,
        scenario_id=scenario,
        title=title,
        summary=summary,
        evidence=evidence,
        confidence=_confidence_for_session(session, events),
        recommended_action=_recommended_action(candidate_type),
    )


def render_obsidian_candidate_note(candidate: EmployeeInsightCandidate) -> str:
    frontmatter = {
        "insight_id": candidate.candidate_id,
        "review_status": _obsidian_review_status(candidate.review_status),
        "candidate_type": candidate.candidate_type.value,
        "department_id": candidate.department_id,
        "scenario_id": candidate.scenario_id,
        "sensitivity": candidate.sensitivity,
        "confidence": candidate.confidence,
        "source_session_ids": candidate.source_session_ids,
        "impact_scope": candidate.impact_scope,
        "runtime_eligible": candidate.is_runtime_eligible,
        "governance_version": 1,
    }
    yaml_text = yaml.safe_dump(
        frontmatter,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    ).strip()
    evidence = "\n".join(
        f"- `{item.get('event_id', '')}` {json.dumps(item, ensure_ascii=False)}"
        for item in candidate.evidence
    )
    return (
        f"---\n{yaml_text}\n---\n\n"
        f"# {candidate.title}\n\n"
        "## 系统归纳\n\n"
        f"{candidate.summary}\n\n"
        "## 员工原始表达与证据\n\n"
        f"{evidence or '- 无结构化证据'}\n\n"
        "## 建议处理\n\n"
        f"{candidate.recommended_action or _recommended_action(candidate.candidate_type)}\n\n"
        "## 影响范围\n\n"
        f"- 部门：{candidate.department_id or '未标注'}\n"
        f"- 场景：{candidate.scenario_id or '未标注'}\n"
        f"- 灰度范围：{candidate.impact_scope}\n\n"
        "## 审批意见\n\n"
        "请将 review_status 改为 approved / rejected / merged / stale / sensitive_blocked，并补充审批原因。\n"
    )


class EmployeeInsightStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self._initialized = False

    async def initialize(self) -> None:
        if self._initialized:
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.executescript(
                """
                CREATE TABLE IF NOT EXISTS employee_insight_sessions (
                    session_id TEXT PRIMARY KEY,
                    employee_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    trigger_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    task_status TEXT NOT NULL,
                    department_id TEXT NOT NULL DEFAULT '',
                    role TEXT NOT NULL DEFAULT '',
                    scenario_id TEXT NOT NULL DEFAULT '',
                    original_request TEXT NOT NULL DEFAULT '',
                    normalized_goal TEXT NOT NULL DEFAULT '',
                    required_inputs_json TEXT NOT NULL DEFAULT '[]',
                    satisfaction TEXT NOT NULL DEFAULT '',
                    friction_points_json TEXT NOT NULL DEFAULT '[]',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    summary TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS employee_insight_events (
                    event_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS employee_insight_candidates (
                    candidate_id TEXT PRIMARY KEY,
                    candidate_type TEXT NOT NULL,
                    source_session_ids_json TEXT NOT NULL,
                    department_id TEXT NOT NULL DEFAULT '',
                    scenario_id TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    review_status TEXT NOT NULL,
                    sensitivity TEXT NOT NULL DEFAULT 'internal',
                    obsidian_note_path TEXT NOT NULL DEFAULT '',
                    recommended_action TEXT NOT NULL DEFAULT '',
                    impact_scope TEXT NOT NULL DEFAULT 'pilot',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS employee_insight_audit (
                    audit_id TEXT PRIMARY KEY,
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    detail_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS employee_insight_profiles (
                    employee_id TEXT PRIMARY KEY,
                    employee_hash TEXT NOT NULL,
                    display_name TEXT NOT NULL DEFAULT '',
                    department_id TEXT NOT NULL DEFAULT '',
                    role TEXT NOT NULL DEFAULT '',
                    pilot_status TEXT NOT NULL,
                    preferred_touch_time TEXT NOT NULL DEFAULT '',
                    last_outreach_at TEXT NOT NULL DEFAULT '',
                    unanswered_outreach_count INTEGER NOT NULL DEFAULT 0,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_employee_insight_sessions_status
                ON employee_insight_sessions(status, updated_at DESC);

                CREATE INDEX IF NOT EXISTS idx_employee_insight_profiles_status
                ON employee_insight_profiles(pilot_status, updated_at DESC);

                CREATE INDEX IF NOT EXISTS idx_employee_insight_events_session
                ON employee_insight_events(session_id, created_at ASC);

                CREATE INDEX IF NOT EXISTS idx_employee_insight_candidates_review
                ON employee_insight_candidates(review_status, updated_at DESC);

                CREATE INDEX IF NOT EXISTS idx_employee_insight_audit_target
                ON employee_insight_audit(target_id, created_at ASC);
                """
            )
            await db.commit()
        self._initialized = True

    async def upsert_profile(self, profile: EmployeeInsightProfile) -> None:
        await self.initialize()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO employee_insight_profiles (
                    employee_id, employee_hash, display_name, department_id, role,
                    pilot_status, preferred_touch_time, last_outreach_at,
                    unanswered_outreach_count, metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(employee_id) DO UPDATE SET
                    employee_hash = excluded.employee_hash,
                    display_name = excluded.display_name,
                    department_id = excluded.department_id,
                    role = excluded.role,
                    pilot_status = excluded.pilot_status,
                    preferred_touch_time = excluded.preferred_touch_time,
                    last_outreach_at = excluded.last_outreach_at,
                    unanswered_outreach_count = excluded.unanswered_outreach_count,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                _profile_row(profile),
            )
            await db.commit()

    async def get_profile(self, employee_id: str) -> EmployeeInsightProfile | None:
        await self.initialize()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute(
                    """
                    SELECT * FROM employee_insight_profiles
                    WHERE employee_id = ?
                    """,
                    (employee_id,),
                )
            ).fetchone()
        return _profile_from_row(row) if row else None

    async def list_profiles(
        self,
        *,
        pilot_status: PilotStatus | None = None,
        limit: int = 500,
    ) -> list[EmployeeInsightProfile]:
        await self.initialize()
        query = "SELECT * FROM employee_insight_profiles WHERE 1 = 1"
        params: list[Any] = []
        if pilot_status:
            query += " AND pilot_status = ?"
            params.append(pilot_status.value)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute(query, tuple(params))).fetchall()
        return [_profile_from_row(row) for row in rows]

    async def mark_profile_engaged(self, employee_id: str) -> None:
        profile = await self.get_profile(employee_id)
        if profile is None:
            return
        await self.upsert_profile(
            replace(
                profile,
                unanswered_outreach_count=0,
                updated_at=_utcnow(),
            )
        )

    async def upsert_session(self, session: EmployeeInsightSession) -> None:
        await self.initialize()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO employee_insight_sessions (
                    session_id, employee_id, channel, trigger_type, status,
                    task_status, department_id, role, scenario_id, original_request,
                    normalized_goal, required_inputs_json, satisfaction,
                    friction_points_json, metadata_json, summary, created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    employee_id = excluded.employee_id,
                    channel = excluded.channel,
                    trigger_type = excluded.trigger_type,
                    status = excluded.status,
                    task_status = excluded.task_status,
                    department_id = excluded.department_id,
                    role = excluded.role,
                    scenario_id = excluded.scenario_id,
                    original_request = excluded.original_request,
                    normalized_goal = excluded.normalized_goal,
                    required_inputs_json = excluded.required_inputs_json,
                    satisfaction = excluded.satisfaction,
                    friction_points_json = excluded.friction_points_json,
                    metadata_json = excluded.metadata_json,
                    summary = excluded.summary,
                    updated_at = excluded.updated_at
                """,
                _session_row(session),
            )
            await db.commit()

    async def get_session(self, session_id: str) -> EmployeeInsightSession | None:
        await self.initialize()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute(
                    "SELECT * FROM employee_insight_sessions WHERE session_id = ?",
                    (session_id,),
                )
            ).fetchone()
        return _session_from_row(row) if row else None

    async def list_sessions(self, *, limit: int = 500) -> list[EmployeeInsightSession]:
        await self.initialize()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    """
                    SELECT * FROM employee_insight_sessions
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
            ).fetchall()
        return [_session_from_row(row) for row in rows]

    async def find_latest_active_session(
        self,
        employee_id: str,
    ) -> EmployeeInsightSession | None:
        """Find the latest employee session that can still receive feedback.

        Args:
            employee_id: Employee open identifier.

        Returns:
            The latest engaged or blocked session, or None when absent.
        """
        await self.initialize()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute(
                    """
                    SELECT * FROM employee_insight_sessions
                    WHERE employee_id = ? AND status IN (?, ?)
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """,
                    (
                        employee_id,
                        EmployeeInsightSessionStatus.ENGAGED.value,
                        EmployeeInsightSessionStatus.BLOCKED.value,
                    ),
                )
            ).fetchone()
        return _session_from_row(row) if row else None

    async def find_session_by_task_id(
        self,
        task_id: str,
    ) -> EmployeeInsightSession | None:
        """Find the employee session linked to a Harness task.

        Args:
            task_id: Harness task identifier.

        Returns:
            The linked employee insight session, or None when absent.
        """
        await self.initialize()
        if not task_id:
            return None
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute(
                    """
                    SELECT * FROM employee_insight_sessions
                    WHERE json_extract(metadata_json, '$.harness_task_id') = ?
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """,
                    (task_id,),
                )
            ).fetchone()
        return _session_from_row(row) if row else None

    async def append_event(self, event: InsightEvent) -> None:
        await self.initialize()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO employee_insight_events (
                    event_id, session_id, event_type, actor, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.session_id,
                    event.event_type,
                    event.actor,
                    _dumps(event.payload),
                    event.created_at,
                ),
            )
            await db.commit()

    async def list_events(self, session_id: str) -> list[InsightEvent]:
        await self.initialize()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    """
                    SELECT * FROM employee_insight_events
                    WHERE session_id = ?
                    ORDER BY created_at ASC
                    """,
                    (session_id,),
                )
            ).fetchall()
        return [_event_from_row(row) for row in rows]

    async def upsert_candidate(self, candidate: EmployeeInsightCandidate) -> None:
        await self.initialize()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO employee_insight_candidates (
                    candidate_id, candidate_type, source_session_ids_json,
                    department_id, scenario_id, title, summary, evidence_json,
                    confidence, review_status, sensitivity, obsidian_note_path,
                    recommended_action, impact_scope, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(candidate_id) DO UPDATE SET
                    candidate_type = excluded.candidate_type,
                    source_session_ids_json = excluded.source_session_ids_json,
                    department_id = excluded.department_id,
                    scenario_id = excluded.scenario_id,
                    title = excluded.title,
                    summary = excluded.summary,
                    evidence_json = excluded.evidence_json,
                    confidence = excluded.confidence,
                    review_status = excluded.review_status,
                    sensitivity = excluded.sensitivity,
                    obsidian_note_path = excluded.obsidian_note_path,
                    recommended_action = excluded.recommended_action,
                    impact_scope = excluded.impact_scope,
                    updated_at = excluded.updated_at
                """,
                _candidate_row(candidate),
            )
            await db.commit()

    async def get_candidate(self, candidate_id: str) -> EmployeeInsightCandidate | None:
        await self.initialize()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute(
                    """
                    SELECT * FROM employee_insight_candidates
                    WHERE candidate_id = ?
                    """,
                    (candidate_id,),
                )
            ).fetchone()
        return _candidate_from_row(row) if row else None

    async def list_candidates(
        self,
        *,
        review_status: ReviewStatus | None = None,
        limit: int = 500,
    ) -> list[EmployeeInsightCandidate]:
        await self.initialize()
        query = "SELECT * FROM employee_insight_candidates WHERE 1 = 1"
        params: list[Any] = []
        if review_status:
            query += " AND review_status = ?"
            params.append(review_status.value)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute(query, tuple(params))).fetchall()
        return [_candidate_from_row(row) for row in rows]

    async def record_audit(
        self,
        *,
        action: str,
        actor: str,
        target_id: str,
        detail: dict[str, Any] | None = None,
    ) -> AuditEvent:
        await self.initialize()
        event = AuditEvent(
            audit_id=uuid.uuid4().hex,
            action=action,
            actor=actor,
            target_id=target_id,
            detail=detail or {},
        )
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO employee_insight_audit (
                    audit_id, action, actor, target_id, detail_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event.audit_id,
                    event.action,
                    event.actor,
                    event.target_id,
                    _dumps(event.detail),
                    event.created_at,
                ),
            )
            await db.commit()
        return event

    async def list_audit_events(self, target_id: str) -> list[AuditEvent]:
        await self.initialize()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    """
                    SELECT * FROM employee_insight_audit
                    WHERE target_id = ?
                    ORDER BY created_at ASC
                    """,
                    (target_id,),
                )
            ).fetchall()
        return [_audit_from_row(row) for row in rows]


@dataclass(slots=True)
class EmployeeInsightObsidianExporter:
    vault_path: Path
    governance_rel_dir: Path = Path("40_MemoryGovernance/EmployeeInsight/Inbox")

    async def export_candidate(
        self,
        store: EmployeeInsightStore,
        candidate: EmployeeInsightCandidate,
        *,
        actor: str = "system",
    ) -> EmployeeInsightCandidate:
        note_dir = self.vault_path / self.governance_rel_dir
        note_dir.mkdir(parents=True, exist_ok=True)
        note_path = note_dir / _safe_note_filename(candidate)
        note_path.write_text(
            render_obsidian_candidate_note(candidate), encoding="utf-8"
        )
        rel_path = str(note_path.relative_to(self.vault_path.parent))
        exported = replace(
            candidate,
            obsidian_note_path=rel_path,
            updated_at=_utcnow(),
        )
        await store.upsert_candidate(exported)
        await store.record_audit(
            action="governance_exported",
            actor=actor,
            target_id=candidate.candidate_id,
            detail={
                "obsidian_note_path": rel_path,
                "review_status": _obsidian_review_status(candidate.review_status),
            },
        )
        return exported


@dataclass(slots=True)
class EmployeeInsightOutreachScheduler:
    store: EmployeeInsightStore
    cooldown_after_unanswered: int = 2
    unanswered_cooldown_days: int = 3

    async def build_daily_plan(
        self,
        *,
        now: str | None = None,
        limit: int = 20,
    ) -> dict[str, list[dict[str, Any]]]:
        now_value = now or _utcnow()
        profiles = await self.store.list_profiles(limit=1000)
        eligible: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for profile in profiles:
            reason = self._skip_reason(profile, now_value)
            item = {
                "employee_id": profile.employee_id,
                "employee_hash": profile.employee_hash,
                "display_name": profile.display_name,
                "department_id": profile.department_id,
                "role": profile.role,
                "pilot_status": profile.pilot_status.value,
                "last_outreach_at": profile.last_outreach_at,
                "unanswered_outreach_count": profile.unanswered_outreach_count,
            }
            if reason:
                skipped.append({**item, "reason": reason})
                continue
            if len(eligible) < limit:
                eligible.append({**item, "reason": "eligible"})
            else:
                skipped.append({**item, "reason": "plan_limit_reached"})
        return {"eligible": eligible, "skipped": skipped}

    async def record_outreach(
        self,
        *,
        employee_id: str,
        message_text: str,
        now: str | None = None,
        actor: str = "employee_insight_scheduler",
    ) -> EmployeeInsightSession:
        now_value = now or _utcnow()
        profile = await self.store.get_profile(employee_id)
        if profile is None:
            raise ValueError(f"employee profile not found: {employee_id}")
        reason = self._skip_reason(profile, now_value)
        if reason:
            raise ValueError(f"outreach not allowed: {reason}")

        session = EmployeeInsightSession(
            session_id=uuid.uuid4().hex,
            employee_id=profile.employee_id,
            channel="lark_dm",
            trigger_type="daily_outreach",
            status=EmployeeInsightSessionStatus.SENT,
            department_id=profile.department_id,
            role=profile.role,
            scenario_id="daily_outreach",
            original_request=message_text[:4000],
            normalized_goal="每日主动触达员工，陪跑一个真实任务。",
            metadata={
                "employee_hash": profile.employee_hash,
                "preferred_touch_time": profile.preferred_touch_time,
                "verification_scope": bool(
                    profile.metadata.get("verification_scope", False)
                ),
                "metric_sample": bool(
                    profile.metadata.get("verification_scope", False)
                ),
            },
            summary="系统按试点名单完成一次主动私聊触达记录。",
            created_at=now_value,
            updated_at=now_value,
        )
        await self.store.upsert_session(session)
        await self.store.append_event(
            InsightEvent(
                event_id=uuid.uuid4().hex,
                session_id=session.session_id,
                event_type="outreach_sent",
                actor=actor,
                payload={
                    "employee_id": profile.employee_id,
                    "employee_hash": profile.employee_hash,
                    "message_text": message_text,
                },
                created_at=now_value,
            )
        )
        await self.store.upsert_profile(
            replace(
                profile,
                last_outreach_at=now_value,
                unanswered_outreach_count=profile.unanswered_outreach_count + 1,
                updated_at=now_value,
            )
        )
        await self.store.record_audit(
            action="outreach_sent",
            actor=actor,
            target_id=session.session_id,
            detail={
                "employee_id": profile.employee_id,
                "employee_hash": profile.employee_hash,
                "channel": "lark_dm",
            },
        )
        return session

    async def record_failed_outreach(
        self,
        *,
        employee_id: str,
        error: str,
        now: str | None = None,
        actor: str = "employee_insight_scheduler",
    ) -> None:
        await self.store.record_audit(
            action="outreach_send_failed",
            actor=actor,
            target_id=employee_id,
            detail={
                "employee_id": employee_id,
                "error": error,
                "created_at": now or _utcnow(),
            },
        )

    def _skip_reason(self, profile: EmployeeInsightProfile, now: str) -> str:
        if profile.pilot_status != PilotStatus.ACTIVE:
            return "pilot_not_active"
        if not profile.last_outreach_at:
            return ""
        last = _parse_iso(profile.last_outreach_at)
        current = _parse_iso(now)
        if last.date() == current.date():
            return "already_contacted_today"
        if profile.unanswered_outreach_count >= self.cooldown_after_unanswered:
            elapsed_days = (current.date() - last.date()).days
            if elapsed_days < self.unanswered_cooldown_days:
                return "cooldown_after_unanswered"
        return ""


@dataclass(slots=True)
class EmployeeInsightOutreachDispatcher:
    store: EmployeeInsightStore
    sender: EmployeeInsightTextSender
    scheduler: EmployeeInsightOutreachScheduler | None = None
    default_message_text: str = (
        "今天想试一个真实工作任务吗？你可以直接把要做的事发给我，"
        "也可以回复：查资料 / 写通知 / 整理文件 / 生成汇报 / 不知道怎么用。"
    )

    async def dispatch_daily_outreach(
        self,
        *,
        now: str | None = None,
        limit: int = 20,
        approved: bool = False,
        dry_run: bool = True,
        actor: str = "employee_insight_dispatcher",
        message_text: str | None = None,
    ) -> dict[str, Any]:
        scheduler = self.scheduler or EmployeeInsightOutreachScheduler(self.store)
        plan = await scheduler.build_daily_plan(now=now, limit=limit)
        if dry_run:
            await self.store.record_audit(
                action="outreach_dry_run",
                actor=actor,
                target_id="daily_outreach",
                detail={
                    "planned_count": len(plan["eligible"]),
                    "now": now or _utcnow(),
                },
            )
            return {
                "mode": "dry_run",
                "planned": plan["eligible"],
                "skipped": plan["skipped"],
                "sent": [],
                "failed": [],
            }
        if not approved:
            raise PermissionError("approved=true is required for real outreach send")

        text = message_text or self.default_message_text
        sent: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        for item in plan["eligible"]:
            employee_id = item["employee_id"]
            result, message_type = await self._send_beginner_message(item, text)
            if not result.success:
                failed.append({"employee_id": employee_id, "error": result.error})
                await scheduler.record_failed_outreach(
                    employee_id=employee_id,
                    error=result.error or "unknown send error",
                    now=now,
                    actor=actor,
                )
                continue
            session = await scheduler.record_outreach(
                employee_id=employee_id,
                message_text=text,
                now=now,
                actor=actor,
            )
            updated = replace(
                session,
                metadata={
                    **session.metadata,
                    "provider_message_id": result.provider_message_id,
                    "message_type": message_type,
                    "send_raw": result.raw,
                },
            )
            await self.store.upsert_session(updated)
            sent.append(
                {
                    "employee_id": employee_id,
                    "session_id": session.session_id,
                    "provider_message_id": result.provider_message_id,
                }
            )
        return {
            "mode": "send",
            "planned": plan["eligible"],
            "skipped": plan["skipped"],
            "sent": sent,
            "failed": failed,
        }

    async def _send_beginner_message(
        self,
        item: dict[str, Any],
        text: str,
    ) -> tuple[TextSendResult, str]:
        employee_id = item["employee_id"]
        card_sender = getattr(self.sender, "send_interactive_card", None)
        if callable(card_sender):
            from dc_engines.feishu_card_streamer import (
                build_employee_insight_welcome_card,
            )

            card_result = await card_sender(
                employee_id,
                build_employee_insight_welcome_card(
                    employee_name=str(item.get("display_name") or ""),
                ),
            )
            if card_result.success:
                return card_result, "interactive_card"
        text_result = await self.sender.send_text(employee_id, text)
        return text_result, "text"


@dataclass(slots=True)
class EmployeeInsightDashboardSnapshot:
    metrics: dict[str, int]
    top_scenarios: list[dict[str, Any]]
    friction_points: list[dict[str, Any]]
    pending_candidates: list[dict[str, Any]]
    hermes_candidates: list[dict[str, Any]]

    @classmethod
    async def from_store(
        cls, store: EmployeeInsightStore
    ) -> EmployeeInsightDashboardSnapshot:
        sessions = await store.list_sessions()
        candidates = await store.list_candidates()
        scenario_counts = Counter(
            session.scenario_id or "unknown" for session in sessions
        )
        friction_counts = Counter(
            point for session in sessions for point in session.friction_points
        )
        pending = [
            candidate
            for candidate in candidates
            if candidate.review_status == ReviewStatus.REVIEW_REQUIRED
        ]
        metrics = {
            "active_pilots": sum(
                1
                for profile in await store.list_profiles()
                if profile.pilot_status == PilotStatus.ACTIVE
            ),
            "total_sessions": len(sessions),
            "outreach_sent": sum(
                1
                for session in sessions
                if session.status
                in {
                    EmployeeInsightSessionStatus.SENT,
                    EmployeeInsightSessionStatus.OPENED,
                    EmployeeInsightSessionStatus.ENGAGED,
                    EmployeeInsightSessionStatus.COMPLETED,
                    EmployeeInsightSessionStatus.BLOCKED,
                }
            ),
            "effective_conversations": sum(
                1
                for session in sessions
                if session.status
                in {
                    EmployeeInsightSessionStatus.ENGAGED,
                    EmployeeInsightSessionStatus.COMPLETED,
                    EmployeeInsightSessionStatus.BLOCKED,
                }
            ),
            "completed_sessions": sum(
                1
                for session in sessions
                if session.status == EmployeeInsightSessionStatus.COMPLETED
            ),
            "blocked_sessions": sum(
                1
                for session in sessions
                if session.status == EmployeeInsightSessionStatus.BLOCKED
            ),
            "pending_candidates": len(pending),
            "released_improvements": sum(
                1
                for candidate in candidates
                if candidate.review_status
                in {ReviewStatus.RELEASED, ReviewStatus.VALIDATED}
            ),
        }
        return cls(
            metrics=metrics,
            top_scenarios=[
                {"scenario_id": scenario, "count": count}
                for scenario, count in scenario_counts.most_common(10)
            ],
            friction_points=[
                {"friction_point": point, "count": count}
                for point, count in friction_counts.most_common(10)
            ],
            pending_candidates=[candidate.to_dict() for candidate in pending[:20]],
            hermes_candidates=[
                candidate.to_dict()
                for candidate in candidates
                if candidate.candidate_type == CandidateType.HERMES_DEEP_DIVE
            ][:20],
        )


@dataclass(slots=True)
class HermesDeepDivePolicy:
    min_support_count: int = 3

    def build_deep_dive_tasks(
        self, candidates: list[EmployeeInsightCandidate]
    ) -> list[dict[str, Any]]:
        grouped: dict[tuple[str, CandidateType], list[EmployeeInsightCandidate]] = (
            defaultdict(list)
        )
        for candidate in candidates:
            if candidate.review_status not in {
                ReviewStatus.REVIEW_REQUIRED,
                ReviewStatus.APPROVED,
            }:
                continue
            if candidate.candidate_type in {
                CandidateType.TEMPLATE,
                CandidateType.ONBOARDING,
            }:
                continue
            grouped[(candidate.scenario_id, candidate.candidate_type)].append(candidate)

        tasks: list[dict[str, Any]] = []
        for (scenario_id, candidate_type), items in grouped.items():
            if len(items) < self.min_support_count:
                continue
            departments = sorted(
                {item.department_id for item in items if item.department_id}
            )
            tasks.append(
                {
                    "task_type": "employee_insight_deep_dive",
                    "scenario_id": scenario_id,
                    "candidate_type": candidate_type.value,
                    "candidate_ids": [item.candidate_id for item in items],
                    "department_ids": departments,
                    "review_status": ReviewStatus.REVIEW_REQUIRED.value,
                    "instruction": (
                        "请基于员工需求洞察候选，深挖真实卡点、产品改造建议、"
                        "模板/skill/router 影响范围，并输出可进入 Obsidian 审批的方案。"
                    ),
                }
            )
        return tasks


def _candidate_type_for_session(session: EmployeeInsightSession) -> CandidateType:
    points = set(session.friction_points)
    if "knowledge_gap" in points or "source_missing" in points:
        return CandidateType.KNOWLEDGE_GAP
    if "unknown_how_to_start" in points:
        return CandidateType.ONBOARDING
    if "intent_not_routed" in points:
        return CandidateType.ROUTER
    if "needs_new_skill" in points:
        return CandidateType.SKILL
    if "needs_deep_dive" in points:
        return CandidateType.HERMES_DEEP_DIVE
    if session.scenario_id in {"write_notice", "generate_report", "generate_sop"}:
        return CandidateType.TEMPLATE
    return CandidateType.NEED


def _candidate_title(candidate_type: CandidateType, scenario_id: str) -> str:
    names = {
        CandidateType.NEED: "员工真实需求候选",
        CandidateType.TEMPLATE: "任务模板优化候选",
        CandidateType.SKILL: "同事 skill 候选",
        CandidateType.ROUTER: "router 意图优化候选",
        CandidateType.KNOWLEDGE_GAP: "知识库缺口候选",
        CandidateType.ONBOARDING: "新手引导优化候选",
        CandidateType.HERMES_DEEP_DIVE: "Hermes 深挖候选",
    }
    return f"{names[candidate_type]}：{scenario_id}"


def _recommended_action(candidate_type: CandidateType) -> str:
    actions = {
        CandidateType.NEED: "进入产品需求池，确认是否需要纳入迭代。",
        CandidateType.TEMPLATE: "审批通过后更新任务模板库，并先在试点员工范围灰度。",
        CandidateType.SKILL: "进入 skill 设计，生成独立开发计划并保留人工评审。",
        CandidateType.ROUTER: "生成 router alias/rule 候选，测试通过后灰度发布。",
        CandidateType.KNOWLEDGE_GAP: "补充或修订知识库资料，经过 Obsidian 治理后再进入生产召回。",
        CandidateType.ONBOARDING: "更新飞书私聊欢迎语、快捷问题和新手陪跑话术。",
        CandidateType.HERMES_DEEP_DIVE: "创建 Hermes 深挖任务，输出仍需回到 Obsidian 审批。",
    }
    return actions[candidate_type]


def _safe_note_filename(candidate: EmployeeInsightCandidate) -> str:
    title = "".join(
        char if char.isalnum() or char in {"-", "_"} else "-"
        for char in candidate.title.strip()
    ).strip("-")
    if not title:
        title = "员工需求洞察"
    return f"{candidate.candidate_id}-{title[:60]}.md"


def _confidence_for_session(
    session: EmployeeInsightSession, events: list[InsightEvent]
) -> float:
    score = 0.55
    if session.status == EmployeeInsightSessionStatus.COMPLETED:
        score += 0.1
    if session.friction_points:
        score += 0.15
    if events:
        score += 0.1
    if session.satisfaction in {"needs_revision", "not_useful"}:
        score += 0.05
    return min(score, 0.95)


def _obsidian_review_status(status: ReviewStatus) -> str:
    if status == ReviewStatus.REVIEW_REQUIRED:
        return "need_review"
    return status.value


def _session_row(session: EmployeeInsightSession) -> tuple[Any, ...]:
    return (
        session.session_id,
        session.employee_id,
        session.channel,
        session.trigger_type,
        session.status.value,
        session.task_status.value,
        session.department_id,
        session.role,
        session.scenario_id,
        session.original_request,
        session.normalized_goal,
        _dumps(session.required_inputs),
        session.satisfaction,
        _dumps(session.friction_points),
        _dumps(session.metadata),
        session.summary,
        session.created_at,
        session.updated_at,
    )


def _profile_row(profile: EmployeeInsightProfile) -> tuple[Any, ...]:
    return (
        profile.employee_id,
        profile.employee_hash,
        profile.display_name,
        profile.department_id,
        profile.role,
        profile.pilot_status.value,
        profile.preferred_touch_time,
        profile.last_outreach_at,
        profile.unanswered_outreach_count,
        _dumps(profile.metadata),
        profile.created_at,
        profile.updated_at,
    )


def _candidate_row(candidate: EmployeeInsightCandidate) -> tuple[Any, ...]:
    return (
        candidate.candidate_id,
        candidate.candidate_type.value,
        _dumps(candidate.source_session_ids),
        candidate.department_id,
        candidate.scenario_id,
        candidate.title,
        candidate.summary,
        _dumps(candidate.evidence),
        candidate.confidence,
        candidate.review_status.value,
        candidate.sensitivity,
        candidate.obsidian_note_path,
        candidate.recommended_action,
        candidate.impact_scope,
        candidate.created_at,
        candidate.updated_at,
    )


def _session_from_row(row: sqlite3.Row | aiosqlite.Row) -> EmployeeInsightSession:
    return EmployeeInsightSession(
        session_id=row["session_id"],
        employee_id=row["employee_id"],
        channel=row["channel"],
        trigger_type=row["trigger_type"],
        status=EmployeeInsightSessionStatus(row["status"]),
        task_status=TaskStatus(row["task_status"]),
        department_id=row["department_id"],
        role=row["role"],
        scenario_id=row["scenario_id"],
        original_request=row["original_request"],
        normalized_goal=row["normalized_goal"],
        required_inputs=_loads(row["required_inputs_json"], []),
        satisfaction=row["satisfaction"],
        friction_points=_loads(row["friction_points_json"], []),
        metadata=_loads(row["metadata_json"], {}),
        summary=row["summary"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _profile_from_row(row: sqlite3.Row | aiosqlite.Row) -> EmployeeInsightProfile:
    return EmployeeInsightProfile(
        employee_id=row["employee_id"],
        employee_hash=row["employee_hash"],
        display_name=row["display_name"],
        department_id=row["department_id"],
        role=row["role"],
        pilot_status=PilotStatus(row["pilot_status"]),
        preferred_touch_time=row["preferred_touch_time"],
        last_outreach_at=row["last_outreach_at"],
        unanswered_outreach_count=int(row["unanswered_outreach_count"]),
        metadata=_loads(row["metadata_json"], {}),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _event_from_row(row: sqlite3.Row | aiosqlite.Row) -> InsightEvent:
    return InsightEvent(
        event_id=row["event_id"],
        session_id=row["session_id"],
        event_type=row["event_type"],
        actor=row["actor"],
        payload=_loads(row["payload_json"], {}),
        created_at=row["created_at"],
    )


def _candidate_from_row(
    row: sqlite3.Row | aiosqlite.Row,
) -> EmployeeInsightCandidate:
    return EmployeeInsightCandidate(
        candidate_id=row["candidate_id"],
        candidate_type=CandidateType(row["candidate_type"]),
        source_session_ids=_loads(row["source_session_ids_json"], []),
        department_id=row["department_id"],
        scenario_id=row["scenario_id"],
        title=row["title"],
        summary=row["summary"],
        evidence=_loads(row["evidence_json"], []),
        confidence=float(row["confidence"]),
        review_status=ReviewStatus(row["review_status"]),
        sensitivity=row["sensitivity"],
        obsidian_note_path=row["obsidian_note_path"],
        recommended_action=row["recommended_action"],
        impact_scope=row["impact_scope"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _audit_from_row(row: sqlite3.Row | aiosqlite.Row) -> AuditEvent:
    return AuditEvent(
        audit_id=row["audit_id"],
        action=row["action"],
        actor=row["actor"],
        target_id=row["target_id"],
        detail=_loads(row["detail_json"], {}),
        created_at=row["created_at"],
    )


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _loads(text: str, default: Any) -> Any:
    try:
        return json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return default


def _utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_iso(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    return datetime.fromisoformat(normalized)
