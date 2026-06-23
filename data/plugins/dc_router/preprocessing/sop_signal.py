"""Employee SOP signal capture for low-friction governed memory intake."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from dc_engines.department_workflows import match_department_workflow
from dc_engines.memory_governance.exporter import export_content_sop_memory_candidate
from dc_engines.memory_governance.models import ReviewDecision
from dc_engines.memory_governance.obsidian_codec import render_governance_note
from dc_engines.memory_governance.store import MemoryGovernanceStore
from dc_engines.spiral_evolution import (
    analyze_employee_sop_signal,
    build_employee_sop_memory_candidate,
    build_low_friction_sop_confirmation,
)

from astrbot.api import logger

from ..paths import data_path, project_root

SOP_SIGNAL_SOURCE: Final[str] = "sop_signal_confirmation"
_AUDIT_PATH: Final[Path] = data_path("sop_signal_capture_audit.jsonl")
_PENDING_TTL_SEC: Final[float] = 900.0
_DEFAULT_GOVERNED_DB: Final[Path] = data_path("governed_memory.db")
_DEFAULT_OBSIDIAN_VAULT: Final[Path] = project_root() / "ObsidianVault"


@dataclass(slots=True)
class SopSignalPendingState:
    signal_id: str
    conversation_id: str
    original_text: str
    signal: dict[str, Any]
    created_at: float
    status: str = "suggested"


@dataclass(slots=True)
class SopSignalDecision:
    stop: bool = False
    signal: dict[str, Any] | None = None
    pending_state: SopSignalPendingState | None = None


@dataclass(slots=True)
class SopSignalActionResult:
    handled: bool
    stop: bool = False
    action: str = ""
    memory_id: str = ""
    note_path: str = ""


_PENDING: dict[str, SopSignalPendingState] = {}


def try_capture_sop_signal(
    event: Any,
    *,
    text: str,
) -> SopSignalDecision:
    """Detect a reusable employee operating habit and stage a confirmation."""

    clean_text = (text or "").strip()
    if not clean_text or clean_text.startswith("__card_action__:"):
        return SopSignalDecision()

    match = match_department_workflow(
        text=clean_text,
        min_score=12,
        allow_department_only=True,
    )
    department_id = match.department_id if match is not None else ""
    scenario_id = match.scenario_id if match is not None else ""
    source_task_id = _source_task_id(event)
    signal = analyze_employee_sop_signal(
        clean_text,
        department_id=department_id,
        scenario_id=scenario_id,
        source_task_id=source_task_id,
        actor_id=_sender_id(event),
    )
    if signal.get("status") != "candidate":
        return SopSignalDecision()

    session_key = _pending_key(event)
    state = SopSignalPendingState(
        signal_id=str(
            build_employee_sop_memory_candidate(signal).get("candidate_id") or ""
        ),
        conversation_id=session_key,
        original_text=clean_text,
        signal=signal,
        created_at=time.monotonic(),
    )
    _PENDING[session_key] = state
    _append_audit(
        action="suggest",
        state=state,
        status_before="",
        status_after="suggested",
    )
    return SopSignalDecision(stop=True, signal=signal, pending_state=state)


def build_sop_signal_confirmation_card(state: SopSignalPendingState) -> dict[str, Any]:
    confirmation = build_low_friction_sop_confirmation(state.signal)
    base = {"source": SOP_SIGNAL_SOURCE, "signal_id": state.signal_id}
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {
                "tag": "plain_text",
                "content": str(confirmation["title"]),
            },
        },
        "elements": [
            {
                "tag": "markdown",
                "content": str(confirmation["message"]),
            },
            {
                "tag": "action",
                "actions": [
                    _button("记住", {**base, "action": "remember"}, "primary"),
                    _button("只这次", {**base, "action": "once"}),
                    _button("不用", {**base, "action": "dismiss"}),
                ],
            },
        ],
    }


def sop_signal_prompt_text(state: SopSignalPendingState) -> str:
    confirmation = build_low_friction_sop_confirmation(state.signal)
    return (
        f"{confirmation['title']}\n"
        f"{confirmation['message']}\n"
        "回复「记住」「只这次」或「不用」即可。"
    )


def try_handle_sop_signal_card_action(
    event: Any,
    value: dict[str, Any],
    *,
    governed_memory_db_path: Path | str | None = None,
    obsidian_vault_path: Path | str | None = None,
) -> SopSignalActionResult:
    """Resolve a trusted SOP signal confirmation card action."""

    if value.get("source") != SOP_SIGNAL_SOURCE:
        return SopSignalActionResult(handled=False)
    action = str(value.get("action") or "")
    if action not in {"remember", "once", "dismiss"}:
        return SopSignalActionResult(handled=True, stop=True, action=action)

    session_key = _pending_key(event)
    pending = _PENDING.get(session_key)
    if pending is None or pending.signal_id != str(value.get("signal_id") or ""):
        _silent_stop(event)
        return SopSignalActionResult(handled=True, stop=True, action=action)
    age = time.monotonic() - pending.created_at
    if age > _PENDING_TTL_SEC:
        _PENDING.pop(session_key, None)
        _append_audit(
            action="expire",
            state=pending,
            status_before=pending.status,
            status_after="expired",
            payload={"age_sec": round(age, 3)},
        )
        _reply(event, "这条记忆确认已经过期了，你可以重新说一遍。")
        return SopSignalActionResult(handled=True, stop=True, action=action)

    _PENDING.pop(session_key, None)
    if action != "remember":
        _append_audit(
            action=action,
            state=pending,
            status_before=pending.status,
            status_after=action,
        )
        _reply(event, "收到，这次我不会把它记成长期处理习惯。")
        return SopSignalActionResult(handled=True, stop=True, action=action)

    candidate = build_employee_sop_memory_candidate(pending.signal)
    now = _now()
    store = MemoryGovernanceStore(governed_memory_db_path or _DEFAULT_GOVERNED_DB)
    export = export_content_sop_memory_candidate(
        candidate=candidate,
        vault_path=obsidian_vault_path or _DEFAULT_OBSIDIAN_VAULT,
        store=store,
        now=now,
    )
    memory_id = (
        export.memory_ids[0] if export.memory_ids else str(candidate["candidate_id"])
    )
    note_path = str(export.note_paths[0]) if export.note_paths else ""
    reviewer = _sender_id(event) or "employee"
    auto_approve_employee_confirmed_memory(
        store=store,
        memory_id=memory_id,
        reviewer=reviewer,
        now=now,
    )
    store.append_audit(
        memory_id,
        "employee_sop_signal_confirmed",
        actor=reviewer,
        payload={
            "source": SOP_SIGNAL_SOURCE,
            "signal_id": pending.signal_id,
            "department_id": pending.signal.get("department_id") or "",
            "scenario_id": pending.signal.get("scenario_id") or "",
            "note_path": note_path,
            "exported_count": export.exported_count,
            "skipped_count": export.skipped_count,
            "auto_approved": True,
        },
    )
    _append_audit(
        action="remember",
        state=pending,
        status_before=pending.status,
        status_after="approved",
        payload={"memory_id": memory_id, "note_path": note_path},
    )
    _reply(
        event, "已记住。它会作为已确认的处理习惯参与后续沉淀，但不会直接改系统规则。"
    )
    return SopSignalActionResult(
        handled=True,
        stop=True,
        action=action,
        memory_id=memory_id,
        note_path=note_path,
    )


def auto_approve_employee_confirmed_memory(
    *,
    store: MemoryGovernanceStore,
    memory_id: str,
    reviewer: str,
    now: str,
) -> None:
    """Approve memory after the triggering employee explicitly confirms it."""

    memory = store.get_memory(memory_id)
    if memory is None or memory.review_status == "approved":
        return
    before = {
        "review_status": memory.review_status,
        "canonical_text": memory.canonical_text,
        "sensitivity": memory.sensitivity,
    }
    memory.review_status = "approved"
    memory.approved_at = now
    memory.approved_by = reviewer
    memory.updated_at = now
    store.upsert_memory(memory)
    after = {
        "review_status": memory.review_status,
        "canonical_text": memory.canonical_text,
        "sensitivity": memory.sensitivity,
    }
    store.record_decision(
        ReviewDecision(
            decision_id=f"dec_{uuid.uuid4().hex}",
            memory_id=memory.memory_id,
            decision="approved",
            reviewer=reviewer,
            reason="Employee explicitly confirmed this operating habit.",
            before=before,
            after=after,
            created_at=now,
        )
    )
    if memory.obsidian_note_path:
        try:
            Path(memory.obsidian_note_path).write_text(
                render_governance_note(memory),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[dc_router] sop signal approved note rewrite skipped: %s", exc
            )


def _button(
    text: str,
    value: dict[str, Any],
    button_type: str = "default",
) -> dict[str, Any]:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": text},
        "type": button_type,
        "value": value,
    }


def _append_audit(
    *,
    action: str,
    state: SopSignalPendingState,
    status_before: str,
    status_after: str,
    payload: dict[str, Any] | None = None,
) -> None:
    record = {
        "actor": "dc_router",
        "action": action,
        "signal_id": state.signal_id,
        "conversation_id": state.conversation_id,
        "status_before": status_before,
        "status_after": status_after,
        "payload": payload or {},
        "timestamp": _now(),
    }
    try:
        _AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _AUDIT_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception as exc:  # noqa: BLE001
        logger.debug("[dc_router] sop signal audit skipped: %s", exc)


def _source_task_id(event: Any) -> str:
    message_obj = getattr(event, "message_obj", None)
    raw_message = getattr(message_obj, "raw_message", None)
    for value in (
        getattr(message_obj, "message_id", ""),
        getattr(raw_message, "message_id", ""),
        getattr(event, "message_id", ""),
    ):
        if str(value or "").strip():
            return str(value).strip()
    return f"{_pending_key(event)}:{hash(str(getattr(event, 'message_str', '') or ''))}"


def _sender_id(event: Any) -> str:
    try:
        return str(event.get_sender_id() or "")
    except Exception:  # noqa: BLE001
        return ""


def _pending_key(event: Any) -> str:
    return f"{getattr(event, 'unified_msg_origin', '') or ''}:{_sender_id(event)}"


def _silent_stop(event: Any) -> None:
    try:
        from astrbot.api.event import MessageEventResult

        event.should_call_llm(False)
        event.set_result(MessageEventResult().message("").use_t2i(False).stop_event())
    except Exception:  # noqa: BLE001
        pass


def _reply(event: Any, text: str) -> None:
    try:
        from astrbot.api.event import MessageEventResult

        event.should_call_llm(False)
        event.set_result(MessageEventResult().message(text).use_t2i(False).stop_event())
    except Exception:  # noqa: BLE001
        pass


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


__all__ = [
    "SOP_SIGNAL_SOURCE",
    "SopSignalActionResult",
    "SopSignalDecision",
    "SopSignalPendingState",
    "auto_approve_employee_confirmed_memory",
    "build_sop_signal_confirmation_card",
    "sop_signal_prompt_text",
    "try_capture_sop_signal",
    "try_handle_sop_signal_card_action",
]
