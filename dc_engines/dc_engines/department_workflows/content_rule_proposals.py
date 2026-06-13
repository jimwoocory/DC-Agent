from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from .content_rule_overrides import (
    ContentSopRuleApplyResult,
    apply_rule_proposal_to_overrides,
    rollback_rule_override,
)

DC_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_RULE_PROPOSALS_DB_PATH = DC_ROOT / "data" / "content_sop_rule_proposals.db"

ProposalStatus = Literal[
    "pending",
    "approved_for_runtime",
    "rejected",
    "applied",
    "rolled_back",
]


@dataclass(slots=True)
class ContentSopRuleProposal:
    proposal_id: str
    status: ProposalStatus
    department_id: str
    scenario_id: str
    rule_type: str
    rule_text: str
    support_count: int
    evidence_candidate_ids: list[str]
    source_payload: dict[str, Any]
    created_at: str
    updated_at: str
    reviewed_by: str = ""
    reviewed_at: str = ""
    applied_at: str = ""
    rolled_back_at: str = ""

    def to_apply_payload(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "status": self.status,
            "department_id": self.department_id,
            "scenario_id": self.scenario_id,
            "rule_type": self.rule_type,
            "rule_text": self.rule_text,
            "support_count": self.support_count,
            "evidence_candidate_ids": list(self.evidence_candidate_ids),
        }

    def to_card_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "status": self.status,
            "department_id": self.department_id,
            "scenario_id": self.scenario_id,
            "rule_type": self.rule_type,
            "rule_text": self.rule_text,
            "support_count": self.support_count,
            "evidence_candidate_ids": list(self.evidence_candidate_ids),
            "updated_at": self.updated_at,
        }


@dataclass(slots=True)
class ContentSopRuleAuditEvent:
    audit_id: str
    proposal_id: str
    action: str
    actor: str
    detail: dict[str, Any]
    created_at: str


class ContentSopRuleProposalStore:
    def __init__(self, db_path: str | Path = DEFAULT_RULE_PROPOSALS_DB_PATH) -> None:
        self.db_path = Path(db_path)
        self._initialized = False

    def initialize(self) -> None:
        if self._initialized:
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS content_sop_rule_proposals (
                    proposal_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    department_id TEXT NOT NULL,
                    scenario_id TEXT NOT NULL DEFAULT '',
                    rule_type TEXT NOT NULL DEFAULT 'process',
                    rule_text TEXT NOT NULL,
                    support_count INTEGER NOT NULL DEFAULT 0,
                    evidence_candidate_ids_json TEXT NOT NULL DEFAULT '[]',
                    source_payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    reviewed_by TEXT NOT NULL DEFAULT '',
                    reviewed_at TEXT NOT NULL DEFAULT '',
                    applied_at TEXT NOT NULL DEFAULT '',
                    rolled_back_at TEXT NOT NULL DEFAULT ''
                );

                CREATE INDEX IF NOT EXISTS idx_content_sop_rule_proposals_status
                ON content_sop_rule_proposals(status, updated_at DESC);

                CREATE TABLE IF NOT EXISTS content_sop_rule_proposal_audit (
                    audit_id TEXT PRIMARY KEY,
                    proposal_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL DEFAULT '',
                    detail_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_content_sop_rule_proposal_audit_proposal
                ON content_sop_rule_proposal_audit(proposal_id, created_at ASC);
                """
            )
            conn.execute(
                """
                UPDATE content_sop_rule_proposals
                SET status = 'pending'
                WHERE status = 'proposal_pending_verification'
                """
            )
            conn.commit()
        self._initialized = True

    def upsert_proposal(
        self,
        proposal: dict[str, Any],
        *,
        status: ProposalStatus = "pending",
    ) -> ContentSopRuleProposal:
        self.initialize()
        now = _utcnow()
        proposal_id = str(proposal.get("proposal_id") or "").strip() or _proposal_id(
            proposal
        )
        row = {
            "proposal_id": proposal_id,
            "status": status,
            "department_id": str(proposal.get("department_id") or ""),
            "scenario_id": str(proposal.get("scenario_id") or ""),
            "rule_type": str(proposal.get("rule_type") or "process"),
            "rule_text": str(proposal.get("rule_text") or ""),
            "support_count": int(proposal.get("support_count") or 0),
            "evidence_candidate_ids_json": _dumps(
                list(proposal.get("evidence_candidate_ids") or [])
            ),
            "source_payload_json": _dumps(proposal),
            "created_at": now,
            "updated_at": now,
        }
        if not row["department_id"] or not row["rule_text"]:
            raise ValueError("proposal requires department_id and rule_text")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO content_sop_rule_proposals (
                    proposal_id, status, department_id, scenario_id, rule_type,
                    rule_text, support_count, evidence_candidate_ids_json,
                    source_payload_json, created_at, updated_at
                ) VALUES (
                    :proposal_id, :status, :department_id, :scenario_id, :rule_type,
                    :rule_text, :support_count, :evidence_candidate_ids_json,
                    :source_payload_json, :created_at, :updated_at
                )
                ON CONFLICT(proposal_id) DO UPDATE SET
                    department_id = excluded.department_id,
                    scenario_id = excluded.scenario_id,
                    rule_type = excluded.rule_type,
                    rule_text = excluded.rule_text,
                    support_count = excluded.support_count,
                    evidence_candidate_ids_json = excluded.evidence_candidate_ids_json,
                    source_payload_json = excluded.source_payload_json,
                    updated_at = excluded.updated_at
                WHERE content_sop_rule_proposals.status = 'pending'
                """,
                row,
            )
            conn.commit()
        stored = self.get_proposal(proposal_id)
        assert stored is not None
        return stored

    def get_proposal(self, proposal_id: str) -> ContentSopRuleProposal | None:
        self.initialize()
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM content_sop_rule_proposals WHERE proposal_id = ?",
                (proposal_id,),
            ).fetchone()
        return _proposal_from_row(row) if row else None

    def list_proposals(
        self,
        *,
        status: ProposalStatus | str | None = None,
        limit: int = 20,
    ) -> list[ContentSopRuleProposal]:
        self.initialize()
        query = "SELECT * FROM content_sop_rule_proposals WHERE 1 = 1"
        params: list[Any] = []
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY updated_at DESC, proposal_id LIMIT ?"
        params.append(limit)
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(query, params).fetchall()
        return [_proposal_from_row(row) for row in rows]

    def set_status(
        self,
        proposal_id: str,
        status: ProposalStatus,
        *,
        actor: str = "",
        from_statuses: set[ProposalStatus] | None = None,
    ) -> ContentSopRuleProposal:
        self.initialize()
        now = _utcnow()
        reviewed_at = now if status in {"approved_for_runtime", "rejected"} else ""
        applied_at = now if status == "applied" else ""
        rolled_back_at = now if status == "rolled_back" else ""
        status_filter = ""
        params: list[Any] = [
            status,
            actor,
            reviewed_at,
            reviewed_at,
            applied_at,
            applied_at,
            rolled_back_at,
            rolled_back_at,
            now,
            proposal_id,
        ]
        if from_statuses is not None:
            placeholders = ", ".join("?" for _ in from_statuses)
            status_filter = f" AND status IN ({placeholders})"
            params.extend(sorted(from_statuses))
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                f"""
                UPDATE content_sop_rule_proposals
                SET status = ?,
                    reviewed_by = COALESCE(NULLIF(?, ''), reviewed_by),
                    reviewed_at = CASE WHEN ? != '' THEN ? ELSE reviewed_at END,
                    applied_at = CASE WHEN ? != '' THEN ? ELSE applied_at END,
                    rolled_back_at = CASE WHEN ? != '' THEN ? ELSE rolled_back_at END,
                    updated_at = ?
                WHERE proposal_id = ?
                {status_filter}
                """,
                params,
            )
            conn.commit()
        if cursor.rowcount != 1:
            current = self.get_proposal(proposal_id)
            if current is None:
                raise LookupError(f"proposal {proposal_id!r} not found")
            if from_statuses is not None:
                allowed = ", ".join(sorted(from_statuses))
                raise ValueError(
                    f"proposal must be in one of [{allowed}] before {status}; "
                    f"current status is {current.status}"
                )
        proposal = self.get_proposal(proposal_id)
        if proposal is None:
            raise LookupError(f"proposal {proposal_id!r} not found")
        return proposal

    def record_audit(
        self,
        proposal_id: str,
        action: str,
        *,
        actor: str = "",
        detail: dict[str, Any] | None = None,
    ) -> ContentSopRuleAuditEvent:
        self.initialize()
        now = _utcnow()
        audit = ContentSopRuleAuditEvent(
            audit_id=_audit_id(proposal_id, action, now),
            proposal_id=proposal_id,
            action=action,
            actor=actor,
            detail=detail or {},
            created_at=now,
        )
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO content_sop_rule_proposal_audit (
                    audit_id, proposal_id, action, actor, detail_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    audit.audit_id,
                    audit.proposal_id,
                    audit.action,
                    audit.actor,
                    _dumps(audit.detail),
                    audit.created_at,
                ),
            )
            conn.commit()
        return audit

    def list_audit(
        self,
        proposal_id: str,
    ) -> list[ContentSopRuleAuditEvent]:
        self.initialize()
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT * FROM content_sop_rule_proposal_audit
                WHERE proposal_id = ?
                ORDER BY rowid ASC
                """,
                (proposal_id,),
            ).fetchall()
        return [_audit_from_row(row) for row in rows]


def approve_rule_proposal(
    store: ContentSopRuleProposalStore,
    proposal_id: str,
    *,
    reviewer: str,
    allowed_reviewers: set[str] | None = None,
) -> ContentSopRuleProposal:
    assert_reviewer_allowed(reviewer, allowed_reviewers)
    proposal = store.set_status(
        proposal_id,
        "approved_for_runtime",
        actor=reviewer,
        from_statuses={"pending"},
    )
    store.record_audit(
        proposal_id,
        "approved_for_runtime",
        actor=reviewer,
        detail={
            "department_id": proposal.department_id,
            "scenario_id": proposal.scenario_id,
        },
    )
    return proposal


def reject_rule_proposal(
    store: ContentSopRuleProposalStore,
    proposal_id: str,
    *,
    reviewer: str,
    allowed_reviewers: set[str] | None = None,
    reason: str = "",
) -> ContentSopRuleProposal:
    assert_reviewer_allowed(reviewer, allowed_reviewers)
    proposal = store.set_status(
        proposal_id,
        "rejected",
        actor=reviewer,
        from_statuses={"pending"},
    )
    store.record_audit(
        proposal_id,
        "rejected",
        actor=reviewer,
        detail={"reason": reason},
    )
    return proposal


def apply_approved_rule_proposal(
    store: ContentSopRuleProposalStore,
    proposal_id: str,
    *,
    overrides_path: Path | str | None = None,
    reviewer: str,
    allowed_reviewers: set[str] | None = None,
    now: str | None = None,
) -> ContentSopRuleApplyResult:
    assert_reviewer_allowed(reviewer, allowed_reviewers)
    proposal = store.get_proposal(proposal_id)
    if proposal is None:
        raise LookupError(f"proposal {proposal_id!r} not found")
    if proposal.status != "approved_for_runtime":
        raise ValueError("proposal must be approved_for_runtime before apply")
    result = apply_rule_proposal_to_overrides(
        proposal.to_apply_payload(),
        path=overrides_path,
        actor=reviewer,
        now=now or _utcnow(),
    )
    if result.applied:
        store.set_status(
            proposal_id,
            "applied",
            actor=reviewer,
            from_statuses={"approved_for_runtime"},
        )
        store.record_audit(
            proposal_id,
            "applied",
            actor=reviewer,
            detail=result.to_dict(),
        )
    return result


def rollback_applied_rule_proposal(
    store: ContentSopRuleProposalStore,
    proposal_id: str,
    *,
    overrides_path: Path | str | None = None,
    reviewer: str,
    allowed_reviewers: set[str] | None = None,
    now: str | None = None,
) -> ContentSopRuleApplyResult:
    assert_reviewer_allowed(reviewer, allowed_reviewers)
    proposal = store.get_proposal(proposal_id)
    if proposal is None:
        raise LookupError(f"proposal {proposal_id!r} not found")
    if proposal.status != "applied":
        raise ValueError("proposal must be applied before rollback")
    result = rollback_rule_override(
        proposal_id,
        path=overrides_path,
        actor=reviewer,
        now=now or _utcnow(),
    )
    if result.applied:
        store.set_status(
            proposal_id,
            "rolled_back",
            actor=reviewer,
            from_statuses={"applied"},
        )
        store.record_audit(
            proposal_id,
            "rolled_back",
            actor=reviewer,
            detail=result.to_dict(),
        )
    return result


def assert_reviewer_allowed(
    reviewer: str,
    allowed_reviewers: set[str] | None,
) -> None:
    if allowed_reviewers is None:
        return
    if not reviewer or reviewer not in allowed_reviewers:
        raise PermissionError("reviewer is not allowed to approve content SOP rules")


def build_rule_proposal_review_card(
    proposal: ContentSopRuleProposal,
) -> dict[str, Any]:
    value_base = {
        "source": "content_sop_rule_review",
        "proposal_id": proposal.proposal_id,
    }
    fields = [
        f"部门：{proposal.department_id}",
        f"场景：{proposal.scenario_id or '全部'}",
        f"支持数：{proposal.support_count}",
        f"状态：{proposal.status}",
        f"规则：{proposal.rule_text}",
    ]
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "green",
            "title": {"tag": "plain_text", "content": "内容 SOP 规则候选"},
        },
        "elements": [
            {"tag": "markdown", "content": "\n".join(f"- {item}" for item in fields)},
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "批准并应用"},
                        "type": "primary",
                        "value": {
                            **value_base,
                            "action": "content_sop_rule_approve_apply",
                        },
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "拒绝"},
                        "value": {**value_base, "action": "content_sop_rule_reject"},
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "回滚"},
                        "value": {**value_base, "action": "content_sop_rule_rollback"},
                    },
                ],
            },
        ],
    }


def _proposal_from_row(row: sqlite3.Row) -> ContentSopRuleProposal:
    return ContentSopRuleProposal(
        proposal_id=row["proposal_id"],
        status=row["status"],
        department_id=row["department_id"],
        scenario_id=row["scenario_id"],
        rule_type=row["rule_type"],
        rule_text=row["rule_text"],
        support_count=int(row["support_count"]),
        evidence_candidate_ids=_loads(row["evidence_candidate_ids_json"], []),
        source_payload=_loads(row["source_payload_json"], {}),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        reviewed_by=row["reviewed_by"],
        reviewed_at=row["reviewed_at"],
        applied_at=row["applied_at"],
        rolled_back_at=row["rolled_back_at"],
    )


def _audit_from_row(row: sqlite3.Row) -> ContentSopRuleAuditEvent:
    return ContentSopRuleAuditEvent(
        audit_id=row["audit_id"],
        proposal_id=row["proposal_id"],
        action=row["action"],
        actor=row["actor"],
        detail=_loads(row["detail_json"], {}),
        created_at=row["created_at"],
    )


def _proposal_id(proposal: dict[str, Any]) -> str:
    raw = "|".join(
        [
            str(proposal.get("department_id") or ""),
            str(proposal.get("scenario_id") or ""),
            str(proposal.get("rule_text") or ""),
        ]
    )
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
    return f"content_sop_proposal_{digest}"


def _audit_id(proposal_id: str, action: str, created_at: str) -> str:
    digest = hashlib.sha1(
        f"{proposal_id}:{action}:{created_at}:{uuid4().hex}".encode()
    ).hexdigest()
    return f"audit_{digest[:16]}"


def _utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _loads(value: str, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except json.JSONDecodeError:
        return fallback
