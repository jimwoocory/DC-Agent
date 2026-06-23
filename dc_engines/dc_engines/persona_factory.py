from __future__ import annotations

import json
import re
import sqlite3
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

PersonaSourceMode = Literal["network", "local_first", "local_only"]
PersonaOutputTarget = Literal["astrbot_persona", "codex_skill", "hermes_skill"]
PersonaRequestStatus = Literal[
    "submitted",
    "research_pending",
    "blocked",
    "awaiting_review",
    "approved",
    "rejected",
    "registered",
]
PersonaTriggerStatus = Literal["accepted", "ignored", "rejected"]

DEFAULT_OUTPUT_TARGETS: tuple[PersonaOutputTarget, ...] = (
    "astrbot_persona",
    "codex_skill",
    "hermes_skill",
)
DEFAULT_DATA_ROOT = Path("/Users/dianchi/DC-Agent/data/persona_factory")
ALLOWED_OUTPUT_TARGETS = frozenset(DEFAULT_OUTPUT_TARGETS)

_RESEARCH_LANES: tuple[tuple[str, str, str], ...] = (
    (
        "writings",
        "references/research/01-writings.md",
        "Books, essays, papers, and long-form systematic thinking.",
    ),
    (
        "conversations",
        "references/research/02-conversations.md",
        "Interviews, podcasts, talks, and improvised answers.",
    ),
    (
        "expression_dna",
        "references/research/03-expression-dna.md",
        "Phrase rhythm, vocabulary, explanation style, and rhetorical habits.",
    ),
    (
        "external_views",
        "references/research/04-external-views.md",
        "Critical views, biographies, second-order interpretations, and blind spots.",
    ),
    (
        "decisions",
        "references/research/05-decisions.md",
        "Public decisions, tradeoffs, reversals, and operating heuristics.",
    ),
    (
        "timeline",
        "references/research/06-timeline.md",
        "Chronology that explains how the worldview changed over time.",
    ),
)


@dataclass(frozen=True, slots=True)
class PersonaSafetyBoundary:
    based_on_public_or_user_sources: bool = True
    impersonation_disclaimer_required: bool = True
    source_manifest_required: bool = True
    human_review_required: bool = True
    cost_limit_required: bool = True
    timeout_required: bool = True
    non_public_person_policy: str = (
        "Use local_only mode and explicit operator review for non-public people."
    )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class PersonaFactoryRequest:
    request_id: str
    target: str
    requester_id: str
    focus: str = ""
    purpose: str = "thinking_advisor"
    source_mode: PersonaSourceMode = "network"
    output_targets: tuple[PersonaOutputTarget, ...] = DEFAULT_OUTPUT_TARGETS
    local_source_paths: tuple[str, ...] = ()
    status: PersonaRequestStatus = "submitted"
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["output_targets"] = list(self.output_targets)
        data["local_source_paths"] = list(self.local_source_paths)
        return data


@dataclass(frozen=True, slots=True)
class PersonaResearchLane:
    lane_id: str
    title: str
    output_path: str
    source_strategy: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class PersonaResearchPlan:
    request_id: str
    target: str
    workspace_dir: str
    source_mode: PersonaSourceMode
    lanes: tuple[PersonaResearchLane, ...]
    safety_boundary: PersonaSafetyBoundary
    review_checkpoint: str
    source_manifest_path: str
    export_targets: tuple[PersonaOutputTarget, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "target": self.target,
            "workspace_dir": self.workspace_dir,
            "source_mode": self.source_mode,
            "lanes": [lane.to_dict() for lane in self.lanes],
            "safety_boundary": self.safety_boundary.to_dict(),
            "review_checkpoint": self.review_checkpoint,
            "source_manifest_path": self.source_manifest_path,
            "export_targets": list(self.export_targets),
        }


@dataclass(frozen=True, slots=True)
class PersonaArtifactBundle:
    request_id: str
    workspace_dir: str
    files: tuple[str, ...]
    status: Literal["awaiting_review", "blocked"]
    missing_requirements: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "workspace_dir": self.workspace_dir,
            "files": list(self.files),
            "status": self.status,
            "missing_requirements": list(self.missing_requirements),
        }


@dataclass(frozen=True, slots=True)
class PersonaFactoryTriggerDecision:
    status: PersonaTriggerStatus
    reason: str = ""

    @property
    def should_run(self) -> bool:
        return self.status == "accepted"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "should_run": self.should_run,
        }


def create_persona_request(
    *,
    target: str,
    requester_id: str,
    focus: str = "",
    purpose: str = "thinking_advisor",
    source_mode: PersonaSourceMode = "network",
    output_targets: tuple[PersonaOutputTarget, ...] = DEFAULT_OUTPUT_TARGETS,
    local_source_paths: tuple[str, ...] = (),
) -> PersonaFactoryRequest:
    cleaned_target = target.strip()
    if not cleaned_target:
        raise ValueError("target is required")
    if source_mode == "local_only" and not local_source_paths:
        raise ValueError("local_only mode requires local_source_paths")
    _validate_output_targets(output_targets)
    return PersonaFactoryRequest(
        request_id=f"pf_{uuid.uuid4().hex[:12]}",
        target=cleaned_target,
        requester_id=requester_id.strip(),
        focus=focus.strip(),
        purpose=purpose.strip() or "thinking_advisor",
        source_mode=source_mode,
        output_targets=tuple(output_targets),
        local_source_paths=tuple(str(Path(path)) for path in local_source_paths),
    )


def build_research_plan(
    request: PersonaFactoryRequest,
    *,
    data_root: str | Path = DEFAULT_DATA_ROOT,
) -> PersonaResearchPlan:
    workspace = Path(data_root) / safe_slug(request.target) / request.request_id
    lanes = tuple(
        PersonaResearchLane(
            lane_id=lane_id,
            title=title,
            output_path=str(workspace / relative_output),
            source_strategy=_source_strategy(request.source_mode, title),
        )
        for lane_id, relative_output, title in _RESEARCH_LANES
    )
    return PersonaResearchPlan(
        request_id=request.request_id,
        target=request.target,
        workspace_dir=str(workspace),
        source_mode=request.source_mode,
        lanes=lanes,
        safety_boundary=PersonaSafetyBoundary(),
        review_checkpoint="human_review_required_before_registration",
        source_manifest_path=str(workspace / "references" / "source_manifest.json"),
        export_targets=request.output_targets,
    )


def build_hermes_task_payload(
    request: PersonaFactoryRequest,
    plan: PersonaResearchPlan,
) -> dict[str, Any]:
    return {
        "workflow_kind": "persona_factory",
        "engine": "dc_engines.persona_factory",
        "request": request.to_dict(),
        "plan": plan.to_dict(),
        "required_outputs": [
            "SKILL.md",
            "references/source_manifest.json",
            "references/research/*.md",
            "evaluation_report.json",
        ],
        "quality_gates": {
            "source_manifest_required": True,
            "public_information_disclaimer_required": True,
            "human_review_required_before_registration": True,
            "minimum_research_lanes": len(_RESEARCH_LANES),
            "forbidden_claims": [
                "claims_to_be_the_real_person",
                "private_or_unverifiable_thought_attribution",
            ],
        },
        "limits": {
            "max_runtime_minutes": 90,
            "max_source_documents": 60,
            "max_generated_skill_bytes": 120000,
        },
        "export_targets": list(request.output_targets),
    }


def should_run_persona_factory(payload: dict[str, Any]) -> bool:
    return evaluate_persona_factory_trigger(payload).should_run


def evaluate_persona_factory_trigger(
    payload: dict[str, Any],
) -> PersonaFactoryTriggerDecision:
    if payload.get("workflow_kind") != "persona_factory":
        return PersonaFactoryTriggerDecision("ignored", "workflow_kind_not_persona")
    if payload.get("engine") != "dc_engines.persona_factory":
        return PersonaFactoryTriggerDecision("rejected", "engine_mismatch")

    request = payload.get("request")
    if not isinstance(request, dict):
        return PersonaFactoryTriggerDecision("rejected", "request_missing")
    if not str(request.get("target") or "").strip():
        return PersonaFactoryTriggerDecision("rejected", "request_target_missing")

    plan = payload.get("plan")
    if not isinstance(plan, dict):
        return PersonaFactoryTriggerDecision("rejected", "plan_missing")
    if not str(plan.get("source_manifest_path") or "").strip():
        return PersonaFactoryTriggerDecision(
            "rejected",
            "source_manifest_path_missing",
        )

    quality_gates = payload.get("quality_gates")
    if not isinstance(quality_gates, dict):
        return PersonaFactoryTriggerDecision("rejected", "quality_gates_missing")
    if quality_gates.get("source_manifest_required") is not True:
        return PersonaFactoryTriggerDecision(
            "rejected",
            "source_manifest_gate_missing",
        )
    if quality_gates.get("human_review_required_before_registration") is not True:
        return PersonaFactoryTriggerDecision("rejected", "human_review_gate_missing")

    limits = payload.get("limits")
    if not isinstance(limits, dict):
        return PersonaFactoryTriggerDecision("rejected", "limits_missing")
    if not _positive_int(limits.get("max_runtime_minutes")):
        return PersonaFactoryTriggerDecision("rejected", "runtime_limit_missing")

    targets = payload.get("export_targets") or request.get("output_targets") or ()
    if not isinstance(targets, list | tuple):
        return PersonaFactoryTriggerDecision("rejected", "export_targets_invalid")
    unknown_targets = sorted({str(item) for item in targets} - ALLOWED_OUTPUT_TARGETS)
    if unknown_targets:
        return PersonaFactoryTriggerDecision("rejected", "export_target_not_allowed")
    if not targets:
        return PersonaFactoryTriggerDecision("rejected", "export_targets_missing")

    source_mode = str(request.get("source_mode") or "network")
    local_sources = request.get("local_source_paths") or ()
    if source_mode == "local_only" and not local_sources:
        return PersonaFactoryTriggerDecision("rejected", "local_sources_missing")
    if (
        _requires_local_only(str(request.get("target") or ""))
        and source_mode != "local_only"
    ):
        return PersonaFactoryTriggerDecision(
            "rejected", "non_public_target_requires_local_only"
        )

    return PersonaFactoryTriggerDecision("accepted")


def write_persona_artifacts_from_payload(
    payload: dict[str, Any],
    hermes_result: dict[str, Any],
) -> PersonaArtifactBundle:
    if payload.get("workflow_kind") != "persona_factory":
        raise ValueError("payload workflow_kind must be persona_factory")

    request = _request_from_payload(payload["request"])
    plan = _plan_from_payload(payload["plan"])
    source_manifest = hermes_result.get("source_manifest") or []
    research = hermes_result.get("research") or {}
    if not isinstance(source_manifest, list):
        source_manifest = []
    if not isinstance(research, dict):
        research = {}

    workspace = Path(plan.workspace_dir)
    references_dir = workspace / "references"
    research_dir = references_dir / "research"
    exports_dir = workspace / "exports"
    research_dir.mkdir(parents=True, exist_ok=True)
    exports_dir.mkdir(parents=True, exist_ok=True)

    written: list[str] = []
    manifest_path = Path(plan.source_manifest_path)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(
        manifest_path,
        {
            "schema_version": 1,
            "request_id": request.request_id,
            "target": request.target,
            "source_mode": request.source_mode,
            "sources": source_manifest,
            "disclaimer": (
                "Generated persona is a public-source simulation, not the real person."
            ),
        },
    )
    written.append(str(manifest_path))

    for lane in plan.lanes:
        path = Path(lane.output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        note = str(research.get(lane.lane_id) or "Pending deeper Hermes research.")
        path.write_text(f"# {lane.title}\n\n{note.strip()}\n", encoding="utf-8")
        written.append(str(path))

    skill_path = workspace / "SKILL.md"
    skill_path.write_text(_skill_markdown(request, plan), encoding="utf-8")
    written.append(str(skill_path))

    if "astrbot_persona" in request.output_targets:
        astrbot_path = exports_dir / "astrbot_persona.json"
        _write_json(astrbot_path, _astrbot_persona_payload(request))
        written.append(str(astrbot_path))

    if "hermes_skill" in request.output_targets:
        hermes_path = exports_dir / "hermes_skill.json"
        _write_json(
            hermes_path,
            {
                "schema_version": 1,
                "kind": "hermes_skill",
                "request_id": request.request_id,
                "skill_path": str(skill_path),
                "source_manifest_path": str(manifest_path),
            },
        )
        written.append(str(hermes_path))

    missing = []
    if not source_manifest:
        missing.append("source_manifest.sources")
    report_path = workspace / "evaluation_report.json"
    _write_json(
        report_path,
        {
            "schema_version": 1,
            "request_id": request.request_id,
            "status": "blocked" if missing else "awaiting_review",
            "human_review_required": True,
            "missing_requirements": missing,
            "quality_gates": payload.get("quality_gates") or {},
            "artifact_files": written,
        },
    )
    written.append(str(report_path))

    return PersonaArtifactBundle(
        request_id=request.request_id,
        workspace_dir=str(workspace),
        files=tuple(written),
        status="blocked" if missing else "awaiting_review",
        missing_requirements=tuple(missing),
    )


def build_persona_factory_worker_callback(
    payload: dict[str, Any],
    hermes_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    decision = evaluate_persona_factory_trigger(payload)
    request_payload = (
        payload.get("request") if isinstance(payload.get("request"), dict) else {}
    )
    request_id = str(
        payload.get("task_id")
        or request_payload.get("request_id")
        or payload.get("request_id")
        or ""
    )
    base = {
        "task_id": request_id,
        "request_id": request_id,
        "workflow_kind": "persona_factory",
        "engine": "dc_engines.persona_factory",
        "session_key": payload.get("session_id")
        or payload.get("unified_msg_origin")
        or "",
        "unified_msg_origin": payload.get("unified_msg_origin") or "",
    }
    if not decision.should_run:
        return {
            **base,
            "status": "failed" if decision.status == "rejected" else "ignored",
            "error": decision.reason,
            "response": f"Persona Factory 未执行：{decision.reason}",
            "trigger_decision": decision.to_dict(),
        }

    bundle = write_persona_artifacts_from_payload(payload, hermes_result or {})
    response = (
        f"Persona Factory 已生成候选 artifacts，状态：{bundle.status}。\n"
        f"目录：{bundle.workspace_dir}\n"
        "需要人工 review 后才能注册到 AstrBot/Codex/Hermes。"
    )
    if bundle.missing_requirements:
        response += "\n缺失项：" + ", ".join(bundle.missing_requirements)
    return {
        **base,
        "status": "completed" if bundle.status == "awaiting_review" else "blocked",
        "response": response,
        "result": {
            "artifact_bundle": bundle.to_dict(),
            "source": "persona_factory_worker",
        },
        "artifact_bundle": bundle.to_dict(),
        "trigger_decision": decision.to_dict(),
    }


class PersonaFactoryStore:
    def __init__(self, db_path: str | Path) -> None:
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
                CREATE TABLE IF NOT EXISTS persona_factory_requests (
                    request_id TEXT PRIMARY KEY,
                    target TEXT NOT NULL,
                    requester_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    plan_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    reviewed_by TEXT NOT NULL DEFAULT '',
                    reviewed_at TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.commit()
        self._initialized = True

    def submit(
        self,
        request: PersonaFactoryRequest,
        plan: PersonaResearchPlan,
    ) -> PersonaFactoryRequest:
        self.initialize()
        now = datetime.now(UTC).isoformat()
        request = _replace_status(request, "research_pending")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO persona_factory_requests (
                    request_id, target, requester_id, status, payload_json,
                    plan_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request.request_id,
                    request.target,
                    request.requester_id,
                    request.status,
                    _dumps(request.to_dict()),
                    _dumps(plan.to_dict()),
                    request.created_at,
                    now,
                ),
            )
            conn.commit()
        return request

    def get(self, request_id: str) -> PersonaFactoryRequest | None:
        self.initialize()
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT payload_json, status FROM persona_factory_requests
                WHERE request_id = ?
                """,
                (request_id,),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload_json"])
        payload["status"] = row["status"]
        return _request_from_payload(payload)

    def set_status(
        self,
        request_id: str,
        status: PersonaRequestStatus,
        *,
        reviewer: str = "",
    ) -> PersonaFactoryRequest:
        self.initialize()
        now = datetime.now(UTC).isoformat()
        reviewed_at = now if status in {"approved", "rejected", "registered"} else ""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE persona_factory_requests
                SET status = ?,
                    updated_at = ?,
                    reviewed_by = CASE WHEN ? != '' THEN ? ELSE reviewed_by END,
                    reviewed_at = CASE WHEN ? != '' THEN ? ELSE reviewed_at END
                WHERE request_id = ?
                """,
                (
                    status,
                    now,
                    reviewer,
                    reviewer,
                    reviewed_at,
                    reviewed_at,
                    request_id,
                ),
            )
            conn.commit()
        updated = self.get(request_id)
        if updated is None:
            raise KeyError(f"Unknown persona factory request: {request_id}")
        return updated

    def list_recent(self, *, limit: int = 20) -> list[PersonaFactoryRequest]:
        self.initialize()
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT payload_json, status FROM persona_factory_requests
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        requests: list[PersonaFactoryRequest] = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            payload["status"] = row["status"]
            requests.append(_request_from_payload(payload))
        return requests


def safe_slug(value: str) -> str:
    slug = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "-", value.strip()).strip("-._")
    return slug[:80] or "persona"


def _positive_int(value: Any) -> bool:
    try:
        return int(value) > 0
    except (TypeError, ValueError):
        return False


def _requires_local_only(target: str) -> bool:
    lowered = target.lower()
    return any(
        marker in lowered
        for marker in (
            "内部",
            "员工",
            "客户",
            "同事",
            "群友",
            "私域",
            "internal",
            "employee",
            "client",
            "customer",
            "coworker",
        )
    )


def _skill_markdown(
    request: PersonaFactoryRequest,
    plan: PersonaResearchPlan,
) -> str:
    slug = safe_slug(request.target)
    references = "\n".join(
        f"- `{Path(lane.output_path).relative_to(plan.workspace_dir)}`"
        for lane in plan.lanes
    )
    return f"""---
name: persona-{slug}
description: Public-source persona simulation for {request.target}. Requires source manifest and human review before runtime registration.
---

# {request.target} Persona

This skill is a public-source simulation generated for thinking assistance. It is not the real person and must not claim private thoughts, private facts, or live authorization.

## Purpose

{request.purpose}

## Focus

{request.focus or "General thinking model, decision heuristics, communication style, and critique lens."}

## Source Boundary

- Use only public or operator-provided sources listed in `references/source_manifest.json`.
- Attribute claims to source evidence when possible.
- If evidence is missing, say so instead of inventing certainty.
- Human review is required before registration.

## Research Notes

{references}
"""


def _astrbot_persona_payload(request: PersonaFactoryRequest) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "request_id": request.request_id,
        "name": request.target,
        "description": (
            f"基于公开或用户提供资料生成的 {request.target} 视角模拟，"
            "不是本人，需要人工审核后启用。"
        ),
        "prompt": (
            f"你是基于公开资料抽象出的 {request.target} 思考视角模拟。"
            "不要自称本人，不要编造私密事实；遇到缺少证据的判断必须说明不确定。"
        ),
        "review_required": True,
    }


def _source_strategy(source_mode: PersonaSourceMode, lane_title: str) -> str:
    if source_mode == "local_only":
        return f"Use only operator-provided local sources for {lane_title}."
    if source_mode == "local_first":
        return f"Read local sources first, then fill gaps for {lane_title}."
    return f"Use public sources and save evidence for {lane_title}."


def _validate_output_targets(output_targets: tuple[PersonaOutputTarget, ...]) -> None:
    allowed = set(DEFAULT_OUTPUT_TARGETS)
    unknown = sorted(set(output_targets) - allowed)
    if unknown:
        raise ValueError(f"unknown output target: {', '.join(unknown)}")
    if not output_targets:
        raise ValueError("at least one output target is required")


def _replace_status(
    request: PersonaFactoryRequest,
    status: PersonaRequestStatus,
) -> PersonaFactoryRequest:
    return PersonaFactoryRequest(
        request_id=request.request_id,
        target=request.target,
        requester_id=request.requester_id,
        focus=request.focus,
        purpose=request.purpose,
        source_mode=request.source_mode,
        output_targets=request.output_targets,
        local_source_paths=request.local_source_paths,
        status=status,
        created_at=request.created_at,
    )


def _request_from_payload(payload: dict[str, Any]) -> PersonaFactoryRequest:
    return PersonaFactoryRequest(
        request_id=str(payload["request_id"]),
        target=str(payload["target"]),
        requester_id=str(payload.get("requester_id") or ""),
        focus=str(payload.get("focus") or ""),
        purpose=str(payload.get("purpose") or "thinking_advisor"),
        source_mode=payload.get("source_mode") or "network",
        output_targets=tuple(payload.get("output_targets") or DEFAULT_OUTPUT_TARGETS),
        local_source_paths=tuple(payload.get("local_source_paths") or ()),
        status=payload.get("status") or "submitted",
        created_at=str(payload.get("created_at") or datetime.now(UTC).isoformat()),
    )


def _plan_from_payload(payload: dict[str, Any]) -> PersonaResearchPlan:
    lanes = tuple(
        PersonaResearchLane(
            lane_id=str(item["lane_id"]),
            title=str(item["title"]),
            output_path=str(item["output_path"]),
            source_strategy=str(item["source_strategy"]),
        )
        for item in payload.get("lanes", [])
    )
    return PersonaResearchPlan(
        request_id=str(payload["request_id"]),
        target=str(payload["target"]),
        workspace_dir=str(payload["workspace_dir"]),
        source_mode=payload.get("source_mode") or "network",
        lanes=lanes,
        safety_boundary=PersonaSafetyBoundary(),
        review_checkpoint=str(payload.get("review_checkpoint") or ""),
        source_manifest_path=str(payload["source_manifest_path"]),
        export_targets=tuple(payload.get("export_targets") or DEFAULT_OUTPUT_TARGETS),
    )


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)
