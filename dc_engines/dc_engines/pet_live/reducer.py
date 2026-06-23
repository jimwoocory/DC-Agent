from __future__ import annotations

from .contracts import PetEvent, PetState, PetVisualState

ENERGY_MAX = 100

_STATE_BY_EVENT: dict[str, tuple[PetVisualState, str]] = {
    "feishu_message_received": ("waiting", "curious"),
    "pet_card_action": ("waiting", "curious"),
    "pet_card_viewed": ("idle", "calm"),
    "router_decision_made": ("thinking", "focused"),
    "memory_context_injected": ("focused", "focused"),
    "harness_task_created": ("working", "focused"),
    "harness_task_in_progress": ("working", "focused"),
    "harness_task_completed": ("success", "happy"),
    "harness_task_review_required": ("review", "focused"),
    "harness_task_failed": ("failed", "worried"),
    "hermes_task_running": ("working", "focused"),
    "hermes_task_completed": ("success", "happy"),
    "hermes_task_failed": ("failed", "worried"),
    "assistant_response_sent": ("success", "happy"),
    "assistant_message_observed": ("waiting", "curious"),
    "assistant_response_failed": ("failed", "worried"),
    "obsidian_note_linked": ("review", "focused"),
    "memory_promoted": ("happy", "happy"),
    "idle_timeout": ("sleeping", "calm"),
}

_DISTILLATION_EVENTS = {
    "assistant_distillation_observed",
    "work_habit_updated",
    "memory_signal_promoted",
}


def apply_event(state: PetState, event: PetEvent) -> PetState:
    if event.event_type in _DISTILLATION_EVENTS:
        return _apply_distillation_event(state, event)

    visual_state, emotion = _STATE_BY_EVENT.get(
        event.event_type,
        (state.state, state.emotion),
    )
    changes = {
        "state": visual_state,
        "emotion": emotion,
        "last_active_at": event.created_at or state.last_active_at,
        "updated_at": event.created_at or state.updated_at,
    }

    if event.event_type in {"task_completed", "harness_task_completed"}:
        changes.update(
            {
                "state": "success",
                "emotion": "happy",
                "energy": min(ENERGY_MAX, state.energy + 10),
                "xp": state.xp + 10,
                "coins": state.coins + 1,
            }
        )
    elif event.event_type == "pet_fed":
        changes.update(
            {
                "state": "happy",
                "emotion": "happy",
                "energy": min(ENERGY_MAX, state.energy + 8),
            }
        )
    elif event.event_type == "assistant_message_observed":
        changes["last_signal"] = {
            "event_type": event.event_type,
            "source": event.source,
            "source_ref": event.source_ref.to_dict(),
            "created_at": event.created_at,
            "summary": str(
                event.payload.get("summary")
                or event.payload.get("content")
                or "有小助手回复"
            )[:160],
        }

    return state.with_changes(**changes)


def _apply_distillation_event(state: PetState, event: PetEvent) -> PetState:
    payload = event.payload or {}
    last_signal = {
        "event_type": event.event_type,
        "source": event.source,
        "source_ref": event.source_ref.to_dict(),
        "created_at": event.created_at,
        "summary": str(payload.get("summary") or payload.get("signal") or "")[:160],
    }
    changes = {
        "mood": str(payload.get("mood") or state.mood),
        "focus_level": _bounded_int(
            payload.get("focus_level"),
            default=state.focus_level,
        ),
        "memory_affinity": _bounded_int(
            payload.get("memory_affinity"),
            default=state.memory_affinity,
        ),
        "work_rhythm": str(payload.get("work_rhythm") or state.work_rhythm),
        "last_signal": last_signal,
        "last_active_at": event.created_at or state.last_active_at,
        "updated_at": event.created_at or state.updated_at,
    }
    return state.with_changes(**changes)


def _bounded_int(value: object, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(0, min(100, parsed))
