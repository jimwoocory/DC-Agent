from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

DEFAULT_DC_ROOT = Path("/Users/dianchi/DC-Agent")
DEFAULT_DISTILLATION_DB_PATH = DEFAULT_DC_ROOT / "data" / "assistant_distillation.db"
DEFAULT_LANGUAGE_OVERRIDES_PATH = (
    DEFAULT_DC_ROOT / "data" / "config" / "assistant_language_overrides.json"
)

CandidateKind = Literal["chitchat_keyword", "intent_alias", "tone_template"]
CandidateStatus = Literal["pending", "approved", "rejected", "applied"]
AuditAction = Literal["created", "approved", "rejected", "applied", "rolled_back"]

_NEGATIVE_CHITCHAT_RE = re.compile(
    r"(查|调|写|改|跑|算|搜|找|做|生成|优化|报错|错误|bug|任务|待办|提醒|方案|项目|资料|文件|链接|推文|群)",
    re.IGNORECASE,
)
_SENSITIVE_PRIVACY_RE = re.compile(
    r"(手机号|手机|电话|身份证|密码|token|密钥|工资|薪资|银行卡|账号|住址|地址|open[_-]?id|union[_-]?id)",
    re.IGNORECASE,
)
_TONE_FEEDBACK_RE = re.compile(
    r"(生硬|不礼貌|太官方|像系统|不自然|冷冰冰|人性化|语气|敬语)"
)
_GROUP_HELP_RE = re.compile(r"(拉.{0,3}进群|进群聊|加群|拉群|群聊)")
_WRITING_ALIAS_RE = re.compile(r"(短一点|精简|太啰嗦|润色|优化.{0,6}(推文|文案|话术))")
_PUNCT_RE = re.compile(r"[\s，。！？、~～?!\.,;；:：\"'“”‘’（）()【】\[\]{}<>《》]+")

_CHITCHAT_INTENT_HINTS: dict[str, tuple[str, ...]] = {
    "greeting": ("滴滴", "嗨喽", "哈喽", "在么", "在不在", "在嘛", "在呀"),
    "farewell": ("拜", "88", "拜了"),
    "identity": ("你哪位", "你谁", "叫啥"),
}


@dataclass(slots=True)
class AssistantDistillationCandidate:
    candidate_id: str
    kind: CandidateKind
    status: CandidateStatus
    intent: str
    normalized_text: str
    response_text: str
    pattern: str
    template_name: str
    template_body: str
    rationale: str
    source: str
    evidence: list[dict[str, Any]]
    count: int
    created_at: str
    updated_at: str
    reviewed_by: str
    reviewed_at: str
    applied_at: str


@dataclass(slots=True)
class AssistantDistillationAuditEvent:
    audit_id: str
    candidate_id: str
    action: AuditAction
    reviewer: str
    affected_rules: list[str]
    detail: dict[str, Any]
    created_at: str


class AssistantDistillationStore:
    def __init__(self, db_path: str | Path = DEFAULT_DISTILLATION_DB_PATH) -> None:
        self.db_path = Path(db_path)
        self._initialized = False

    def initialize(self) -> None:
        if self._initialized:
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS assistant_distillation_candidates (
                    candidate_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    intent TEXT NOT NULL DEFAULT '',
                    normalized_text TEXT NOT NULL DEFAULT '',
                    response_text TEXT NOT NULL DEFAULT '',
                    pattern TEXT NOT NULL DEFAULT '',
                    template_name TEXT NOT NULL DEFAULT '',
                    template_body TEXT NOT NULL DEFAULT '',
                    rationale TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT '',
                    evidence_json TEXT NOT NULL DEFAULT '[]',
                    count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    reviewed_by TEXT NOT NULL DEFAULT '',
                    reviewed_at TEXT NOT NULL DEFAULT '',
                    applied_at TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_assistant_distillation_status
                ON assistant_distillation_candidates(status, updated_at DESC)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_assistant_distillation_kind
                ON assistant_distillation_candidates(kind, updated_at DESC)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS assistant_distillation_audit_events (
                    audit_id TEXT PRIMARY KEY,
                    candidate_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    reviewer TEXT NOT NULL DEFAULT '',
                    affected_rules_json TEXT NOT NULL DEFAULT '[]',
                    detail_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_assistant_distillation_audit_candidate
                ON assistant_distillation_audit_events(candidate_id, created_at ASC)
                """
            )
            conn.commit()
        self._initialized = True

    def upsert_candidate(
        self,
        *,
        kind: CandidateKind,
        intent: str = "",
        normalized_text: str = "",
        response_text: str = "",
        pattern: str = "",
        template_name: str = "",
        template_body: str = "",
        rationale: str = "",
        source: str = "",
        evidence: list[dict[str, Any]] | None = None,
        count: int = 1,
    ) -> AssistantDistillationCandidate:
        self.initialize()
        now = _utcnow()
        candidate_id = _candidate_id(
            kind, intent, normalized_text, pattern, template_name
        )
        row = {
            "candidate_id": candidate_id,
            "kind": kind,
            "status": "pending",
            "intent": intent,
            "normalized_text": normalized_text,
            "response_text": response_text,
            "pattern": pattern,
            "template_name": template_name,
            "template_body": template_body,
            "rationale": rationale,
            "source": source,
            "evidence_json": _dumps(evidence or []),
            "count": count,
            "created_at": now,
            "updated_at": now,
        }
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO assistant_distillation_candidates (
                    candidate_id, kind, status, intent, normalized_text,
                    response_text, pattern, template_name, template_body,
                    rationale, source, evidence_json, count, created_at, updated_at
                ) VALUES (
                    :candidate_id, :kind, :status, :intent, :normalized_text,
                    :response_text, :pattern, :template_name, :template_body,
                    :rationale, :source, :evidence_json, :count, :created_at,
                    :updated_at
                )
                ON CONFLICT(candidate_id) DO UPDATE SET
                    intent = excluded.intent,
                    response_text = excluded.response_text,
                    pattern = excluded.pattern,
                    template_name = excluded.template_name,
                    template_body = excluded.template_body,
                    rationale = excluded.rationale,
                    source = excluded.source,
                    evidence_json = excluded.evidence_json,
                    count = excluded.count,
                    updated_at = excluded.updated_at
                WHERE assistant_distillation_candidates.status = 'pending'
                """,
                row,
            )
            conn.commit()
        candidate = self.get_candidate(candidate_id)
        assert candidate is not None
        return candidate

    def get_candidate(
        self,
        candidate_id: str,
    ) -> AssistantDistillationCandidate | None:
        self.initialize()
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT * FROM assistant_distillation_candidates
                WHERE candidate_id = ?
                """,
                (candidate_id,),
            ).fetchone()
        return _candidate_from_row(row) if row else None

    def list_candidates(
        self,
        *,
        status: CandidateStatus | str | None = None,
        limit: int = 50,
    ) -> list[AssistantDistillationCandidate]:
        self.initialize()
        query = "SELECT * FROM assistant_distillation_candidates WHERE 1 = 1"
        params: list[Any] = []
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(query, params).fetchall()
        return [_candidate_from_row(row) for row in rows]

    def set_status(
        self,
        candidate_id: str,
        status: CandidateStatus,
        *,
        reviewer: str = "",
        applied: bool = False,
    ) -> AssistantDistillationCandidate:
        self.initialize()
        now = _utcnow()
        reviewed_at = now if status in {"approved", "rejected"} else ""
        applied_at = now if applied else ""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE assistant_distillation_candidates
                SET status = ?,
                    reviewed_by = COALESCE(NULLIF(?, ''), reviewed_by),
                    reviewed_at = CASE WHEN ? != '' THEN ? ELSE reviewed_at END,
                    applied_at = CASE WHEN ? != '' THEN ? ELSE applied_at END,
                    updated_at = ?
                WHERE candidate_id = ?
                """,
                (
                    status,
                    reviewer,
                    reviewed_at,
                    reviewed_at,
                    applied_at,
                    applied_at,
                    now,
                    candidate_id,
                ),
            )
            conn.commit()
        candidate = self.get_candidate(candidate_id)
        if candidate is None:
            raise LookupError(f"candidate {candidate_id!r} not found")
        return candidate

    def record_audit_event(
        self,
        *,
        candidate_id: str,
        action: AuditAction,
        reviewer: str = "",
        affected_rules: list[str] | None = None,
        detail: dict[str, Any] | None = None,
    ) -> AssistantDistillationAuditEvent:
        self.initialize()
        now = _utcnow()
        audit_id = _audit_id(candidate_id, action, now)
        event = AssistantDistillationAuditEvent(
            audit_id=audit_id,
            candidate_id=candidate_id,
            action=action,
            reviewer=reviewer,
            affected_rules=affected_rules or [],
            detail=detail or {},
            created_at=now,
        )
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO assistant_distillation_audit_events (
                    audit_id, candidate_id, action, reviewer,
                    affected_rules_json, detail_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.audit_id,
                    event.candidate_id,
                    event.action,
                    event.reviewer,
                    _dumps(event.affected_rules),
                    _dumps(event.detail),
                    event.created_at,
                ),
            )
            conn.commit()
        return event

    def list_audit_events(
        self,
        *,
        candidate_id: str | None = None,
        limit: int = 100,
    ) -> list[AssistantDistillationAuditEvent]:
        self.initialize()
        query = "SELECT * FROM assistant_distillation_audit_events WHERE 1 = 1"
        params: list[Any] = []
        if candidate_id:
            query += " AND candidate_id = ?"
            params.append(candidate_id)
        query += " ORDER BY created_at ASC LIMIT ?"
        params.append(limit)
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(query, params).fetchall()
        return [_audit_from_row(row) for row in rows]


def generate_chitchat_candidates_from_miss_log(
    *,
    miss_log_path: Path,
    store: AssistantDistillationStore,
    min_count: int = 3,
) -> dict[str, Any]:
    rows = _load_jsonl(miss_log_path)
    texts = [
        _normalize_short_text(
            str(row.get("normalized_text") or row.get("raw_text") or "")
        )
        for row in rows
    ]
    counter = Counter(text for text in texts if _is_safe_chitchat_candidate_text(text))
    created = 0
    for text, count in counter.items():
        if count < min_count:
            continue
        intent = _infer_chitchat_intent(text)
        if not intent:
            continue
        evidence = [
            row
            for row in rows
            if _normalize_short_text(
                str(row.get("normalized_text") or row.get("raw_text") or "")
            )
            == text
        ][:10]
        store.upsert_candidate(
            kind="chitchat_keyword",
            intent=intent,
            normalized_text=text,
            response_text=_default_chitchat_response(intent),
            source=str(miss_log_path),
            evidence=evidence,
            count=count,
            rationale="High-frequency safe short phrase missed by chitchat guard.",
        )
        created += 1
    return {"source": str(miss_log_path), "created_or_updated": created}


def generate_tone_candidates_from_inbox(
    *,
    inbox_db_path: Path,
    store: AssistantDistillationStore,
    limit: int = 200,
) -> dict[str, Any]:
    rows = _fetch_inbox_rows(inbox_db_path, category="feedback", limit=limit)
    matched = [row for row in rows if _TONE_FEEDBACK_RE.search(row["text"])]
    if not matched:
        return {"source": str(inbox_db_path), "created_or_updated": 0}
    store.upsert_candidate(
        kind="tone_template",
        template_name="employee_test_upgrade_notice",
        template_body=(
            "对员工说明系统升级或测试问题时，先说明不是对方操作问题，"
            "再用公司敬语表达感谢，并轻声邀请继续测试。"
        ),
        rationale="Employee feedback indicates assistant language should be warmer.",
        source=str(inbox_db_path),
        evidence=[dict(row) for row in matched[:10]],
        count=len(matched),
    )
    return {"source": str(inbox_db_path), "created_or_updated": 1}


def generate_intent_candidates_from_inbox(
    *,
    inbox_db_path: Path,
    store: AssistantDistillationStore,
    limit: int = 200,
) -> dict[str, Any]:
    rows = _fetch_inbox_rows(inbox_db_path, category="feedback", limit=limit)
    created = 0
    if any(_GROUP_HELP_RE.search(row["text"]) for row in rows):
        store.upsert_candidate(
            kind="intent_alias",
            intent="writing",
            pattern="拉.{0,3}进群|进群聊|加群|拉群",
            rationale="Employees ask group-invite questions in natural language.",
            source=str(inbox_db_path),
            evidence=[dict(row) for row in rows if _GROUP_HELP_RE.search(row["text"])][
                :10
            ],
            count=sum(1 for row in rows if _GROUP_HELP_RE.search(row["text"])),
        )
        created += 1
    if any(_WRITING_ALIAS_RE.search(row["text"]) for row in rows):
        store.upsert_candidate(
            kind="intent_alias",
            intent="writing",
            pattern="短一点|精简|太啰嗦|润色|优化.{0,6}(推文|文案|话术)",
            rationale="Employees express writing tasks without formal command words.",
            source=str(inbox_db_path),
            evidence=[
                dict(row) for row in rows if _WRITING_ALIAS_RE.search(row["text"])
            ][:10],
            count=sum(1 for row in rows if _WRITING_ALIAS_RE.search(row["text"])),
        )
        created += 1
    return {"source": str(inbox_db_path), "created_or_updated": created}


def approve_candidate(
    store: AssistantDistillationStore,
    candidate_id: str,
    *,
    reviewer: str = "",
    allowed_reviewers: set[str] | None = None,
) -> AssistantDistillationCandidate:
    assert_reviewer_allowed(reviewer, allowed_reviewers)
    candidate = store.set_status(candidate_id, "approved", reviewer=reviewer)
    store.record_audit_event(
        candidate_id=candidate_id,
        action="approved",
        reviewer=reviewer,
        detail={"kind": candidate.kind, "intent": candidate.intent},
    )
    return candidate


def reject_candidate(
    store: AssistantDistillationStore,
    candidate_id: str,
    *,
    reviewer: str = "",
    allowed_reviewers: set[str] | None = None,
    reason: str = "",
) -> AssistantDistillationCandidate:
    assert_reviewer_allowed(reviewer, allowed_reviewers)
    candidate = store.set_status(candidate_id, "rejected", reviewer=reviewer)
    store.record_audit_event(
        candidate_id=candidate_id,
        action="rejected",
        reviewer=reviewer,
        detail={"kind": candidate.kind, "reason": reason},
    )
    return candidate


def apply_candidate(
    store: AssistantDistillationStore,
    candidate_id: str,
    *,
    overrides_path: Path = DEFAULT_LANGUAGE_OVERRIDES_PATH,
    reviewer: str = "",
    allowed_reviewers: set[str] | None = None,
) -> AssistantDistillationCandidate:
    assert_reviewer_allowed(reviewer, allowed_reviewers)
    candidate = store.get_candidate(candidate_id)
    if candidate is None:
        raise LookupError(f"candidate {candidate_id!r} not found")
    if candidate.status != "approved":
        raise ValueError("candidate must be approved before it can be applied")

    overrides = load_language_overrides(overrides_path)
    affected_rules: list[str] = []
    if candidate.kind == "chitchat_keyword":
        keywords = overrides["chitchat"]["keywords"].setdefault(candidate.intent, [])
        if candidate.normalized_text and candidate.normalized_text not in keywords:
            keywords.append(candidate.normalized_text)
            affected_rules.append(
                f"chitchat.{candidate.intent}.{candidate.normalized_text}"
            )
        if candidate.response_text:
            responses = overrides["chitchat"]["responses"].setdefault(
                candidate.intent, []
            )
            if candidate.response_text not in responses:
                responses.append(candidate.response_text)
    elif candidate.kind == "intent_alias":
        item = {
            "pattern": candidate.pattern,
            "intent": candidate.intent,
            "source": candidate.candidate_id,
        }
        aliases = overrides["intent_aliases"]
        if not any(
            alias.get("pattern") == item["pattern"]
            and alias.get("intent") == item["intent"]
            for alias in aliases
            if isinstance(alias, dict)
        ):
            aliases.append(item)
            affected_rules.append(
                f"intent_alias.{candidate.intent}.{candidate.pattern}"
            )
    elif candidate.kind == "tone_template":
        item = {
            "name": candidate.template_name,
            "body": candidate.template_body,
            "source": candidate.candidate_id,
        }
        templates = overrides["tone_templates"]
        if not any(
            template.get("name") == item["name"]
            for template in templates
            if isinstance(template, dict)
        ):
            templates.append(item)
            affected_rules.append(f"tone_template.{candidate.template_name}")
    else:
        raise ValueError(f"unsupported candidate kind: {candidate.kind}")

    save_language_overrides(overrides, overrides_path)
    applied = store.set_status(candidate_id, "applied", reviewer=reviewer, applied=True)
    store.record_audit_event(
        candidate_id=candidate_id,
        action="applied",
        reviewer=reviewer,
        affected_rules=affected_rules,
        detail={"overrides_path": str(overrides_path), "kind": candidate.kind},
    )
    return applied


def assert_reviewer_allowed(
    reviewer: str,
    allowed_reviewers: set[str] | None,
) -> None:
    if allowed_reviewers is None:
        return
    if reviewer not in allowed_reviewers:
        raise PermissionError(f"reviewer {reviewer!r} is not allowed")


def load_audit_events(
    store: AssistantDistillationStore,
    *,
    candidate_id: str | None = None,
    limit: int = 100,
) -> list[AssistantDistillationAuditEvent]:
    return store.list_audit_events(candidate_id=candidate_id, limit=limit)


def collect_distillation_metrics(
    *,
    store: AssistantDistillationStore,
    miss_log_path: Path,
    hit_log_path: Path | None = None,
) -> dict[str, Any]:
    candidates = store.list_candidates(status=None, limit=10000)
    status_counts: dict[str, int] = {}
    kind_counts: dict[str, int] = {}
    for candidate in candidates:
        status_counts[candidate.status] = status_counts.get(candidate.status, 0) + 1
        kind_counts[candidate.kind] = kind_counts.get(candidate.kind, 0) + 1
    miss_rows = _load_jsonl(miss_log_path)
    hit_rows = _load_jsonl(hit_log_path) if hit_log_path else []
    pending_count = status_counts.get("pending", 0)
    approved_count = status_counts.get("approved", 0)
    rejected_count = status_counts.get("rejected", 0)
    reviewed_count = approved_count + rejected_count + status_counts.get("applied", 0)
    hit_count = len(hit_rows)
    miss_count = len(miss_rows)
    hit_rate = hit_count / (hit_count + miss_count) if hit_count + miss_count else 0.0
    false_trigger_rate = rejected_count / reviewed_count if reviewed_count else 0.0
    return {
        "candidate_status_counts": status_counts,
        "candidate_kind_counts": kind_counts,
        "pending_approval_count": pending_count,
        "approval_backlog_count": pending_count,
        "approved_not_applied_count": approved_count,
        "chitchat_hit_count": hit_count,
        "chitchat_miss_count": miss_count,
        "chitchat_hit_rate": round(hit_rate, 4),
        "estimated_false_trigger_rate": round(false_trigger_rate, 4),
        "llm_saved_estimate_count": hit_count,
        "misroute_review_queue_count": sum(
            1 for candidate in candidates if candidate.kind == "intent_alias"
        ),
    }


def build_candidate_review_card(
    candidate: AssistantDistillationCandidate,
) -> dict[str, Any]:
    title = {
        "chitchat_keyword": "短句白名单候选",
        "intent_alias": "意图识别候选",
        "tone_template": "人性化话术候选",
    }.get(candidate.kind, "小助手学习候选")
    preview = candidate.normalized_text or candidate.pattern or candidate.template_body
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": f"小助手学习候选 · {title}"},
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"**候选 ID**：{candidate.candidate_id}\n"
                        f"**类型**：{candidate.kind}\n"
                        f"**意图**：{candidate.intent or '-'}\n"
                        f"**命中次数**：{candidate.count}\n"
                        f"**预览**：{preview or '-'}\n"
                        f"**依据**：{candidate.rationale or '-'}"
                    ),
                },
            },
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "允许加入"},
                        "type": "primary",
                        "value": {
                            "action": "assistant_distillation_approve",
                            "candidate_id": candidate.candidate_id,
                        },
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "拒绝忽略"},
                        "type": "danger",
                        "value": {
                            "action": "assistant_distillation_reject",
                            "candidate_id": candidate.candidate_id,
                        },
                    },
                ],
            },
        ],
    }


def load_language_overrides(
    path: Path = DEFAULT_LANGUAGE_OVERRIDES_PATH,
) -> dict[str, Any]:
    data = _empty_overrides()
    if path.exists():
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                data = _merge_overrides(data, parsed)
        except json.JSONDecodeError:
            return data
    validate_language_overrides(data)
    return data


def save_language_overrides(overrides: dict[str, Any], path: Path) -> None:
    existing_version = 0
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                existing_version = int(existing.get("version") or 0)
        except (json.JSONDecodeError, ValueError, TypeError):
            existing_version = 0
    merged = _merge_overrides(_empty_overrides(), overrides)
    merged["schema_version"] = 1
    merged["version"] = existing_version + 1
    merged["updated_at"] = _utcnow()
    validate_language_overrides(merged)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup_path = _rollback_path(path)
        shutil.copy2(path, backup_path)
    payload = json.dumps(merged, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as tmp:
        tmp.write(payload)
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


def validate_language_overrides(overrides: dict[str, Any]) -> None:
    if not isinstance(overrides, dict):
        raise ValueError("overrides must be a JSON object")
    if int(overrides.get("schema_version") or 1) != 1:
        raise ValueError("schema_version must be 1")
    if int(overrides.get("version") or 0) < 0:
        raise ValueError("version must be a non-negative integer")
    chitchat = overrides.get("chitchat")
    if not isinstance(chitchat, dict):
        raise ValueError("chitchat must be an object")
    for section in ("keywords", "responses"):
        values = chitchat.get(section)
        if not isinstance(values, dict):
            raise ValueError(f"chitchat.{section} must be an object")
        for intent, items in values.items():
            if not isinstance(items, list):
                raise ValueError(f"chitchat.{section}.{intent} must be an array")
            if not all(isinstance(item, str) and item.strip() for item in items):
                raise ValueError(f"chitchat.{section}.{intent} must contain strings")
    aliases = overrides.get("intent_aliases")
    if not isinstance(aliases, list):
        raise ValueError("intent_aliases must be an array")
    for index, alias in enumerate(aliases):
        if not isinstance(alias, dict):
            raise ValueError(f"intent_aliases[{index}] must be an object")
        if not isinstance(alias.get("pattern"), str) or not alias["pattern"].strip():
            raise ValueError(f"intent_aliases[{index}].pattern is required")
        if not isinstance(alias.get("intent"), str) or not alias["intent"].strip():
            raise ValueError(f"intent_aliases[{index}].intent is required")
        try:
            re.compile(alias["pattern"])
        except re.error as exc:
            raise ValueError(f"intent_aliases[{index}].pattern invalid: {exc}") from exc
    templates = overrides.get("tone_templates")
    if not isinstance(templates, list):
        raise ValueError("tone_templates must be an array")
    for index, template in enumerate(templates):
        if not isinstance(template, dict):
            raise ValueError(f"tone_templates[{index}] must be an object")
        if not isinstance(template.get("name"), str) or not template["name"].strip():
            raise ValueError(f"tone_templates[{index}].name is required")
        if not isinstance(template.get("body"), str) or not template["body"].strip():
            raise ValueError(f"tone_templates[{index}].body is required")


def rollback_language_overrides(path: Path, rollback_path: Path) -> None:
    rollback_data = load_language_overrides(rollback_path)
    save_language_overrides(rollback_data, path)


def _fetch_inbox_rows(
    inbox_db_path: Path,
    *,
    category: str,
    limit: int,
) -> list[sqlite3.Row]:
    if not inbox_db_path.exists():
        return []
    with sqlite3.connect(inbox_db_path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            """
            SELECT item_id, session_id, sender_id, sender_name, text, category,
                   status, created_at, updated_at
            FROM inbox_items
            WHERE category = ?
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (category, limit),
        ).fetchall()


def _candidate_from_row(row: sqlite3.Row) -> AssistantDistillationCandidate:
    return AssistantDistillationCandidate(
        candidate_id=row["candidate_id"],
        kind=row["kind"],
        status=row["status"],
        intent=row["intent"],
        normalized_text=row["normalized_text"],
        response_text=row["response_text"],
        pattern=row["pattern"],
        template_name=row["template_name"],
        template_body=row["template_body"],
        rationale=row["rationale"],
        source=row["source"],
        evidence=json.loads(row["evidence_json"] or "[]"),
        count=int(row["count"] or 0),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        reviewed_by=row["reviewed_by"],
        reviewed_at=row["reviewed_at"],
        applied_at=row["applied_at"],
    )


def _audit_from_row(row: sqlite3.Row) -> AssistantDistillationAuditEvent:
    return AssistantDistillationAuditEvent(
        audit_id=row["audit_id"],
        candidate_id=row["candidate_id"],
        action=row["action"],
        reviewer=row["reviewer"],
        affected_rules=json.loads(row["affected_rules_json"] or "[]"),
        detail=json.loads(row["detail_json"] or "{}"),
        created_at=row["created_at"],
    )


def _empty_overrides() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "version": 0,
        "updated_at": "",
        "chitchat": {"keywords": {}, "responses": {}},
        "intent_aliases": [],
        "tone_templates": [],
    }


def _merge_overrides(base: dict[str, Any], parsed: dict[str, Any]) -> dict[str, Any]:
    try:
        base["schema_version"] = int(parsed.get("schema_version") or 1)
    except (TypeError, ValueError):
        base["schema_version"] = 1
    try:
        base["version"] = int(parsed.get("version") or 0)
    except (TypeError, ValueError):
        base["version"] = 0
    base["updated_at"] = str(parsed.get("updated_at") or "")
    chitchat = (
        parsed.get("chitchat") if isinstance(parsed.get("chitchat"), dict) else {}
    )
    for section in ("keywords", "responses"):
        values = chitchat.get(section) if isinstance(chitchat, dict) else {}
        if not isinstance(values, dict):
            continue
        for intent, items in values.items():
            if isinstance(items, list):
                base["chitchat"][section][str(intent)] = [str(item) for item in items]
    aliases = parsed.get("intent_aliases")
    if isinstance(aliases, list):
        base["intent_aliases"] = [
            item
            for item in aliases
            if isinstance(item, dict) and item.get("pattern") and item.get("intent")
        ]
    templates = parsed.get("tone_templates")
    if isinstance(templates, list):
        base["tone_templates"] = [
            item
            for item in templates
            if isinstance(item, dict) and item.get("name") and item.get("body")
        ]
    return base


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            rows.append(parsed)
    return rows


def _infer_chitchat_intent(text: str) -> str:
    for intent, keywords in _CHITCHAT_INTENT_HINTS.items():
        if text in keywords:
            return intent
    return ""


def _is_safe_chitchat_candidate_text(text: str) -> bool:
    if not text or len(text) > 8:
        return False
    if _NEGATIVE_CHITCHAT_RE.search(text):
        return False
    return not _SENSITIVE_PRIVACY_RE.search(text)


def _default_chitchat_response(intent: str) -> str:
    if intent == "farewell":
        return "好的，后续有需要您随时找我。"
    if intent == "identity":
        return "我是巅池-Agent 小助手，可以协助您处理日常工作需求。"
    return "您好，我在的。您可以直接把需要我协助的内容发给我。"


def _normalize_short_text(text: str) -> str:
    return _PUNCT_RE.sub("", text.strip().lower())


def _candidate_id(
    kind: str,
    intent: str,
    normalized_text: str,
    pattern: str,
    template_name: str,
) -> str:
    raw = "|".join((kind, intent, normalized_text, pattern, template_name))
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    return f"assistant:{kind}:{digest}"


def _audit_id(candidate_id: str, action: str, created_at: str) -> str:
    digest = hashlib.sha1(f"{candidate_id}|{action}|{created_at}".encode()).hexdigest()
    return f"audit:{digest[:20]}"


def _rollback_path(path: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return path.with_name(f"{path.name}.rollback-{stamp}")


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
