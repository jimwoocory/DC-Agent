from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Any, Literal

PetVisualState = Literal[
    "idle",
    "waiting",
    "thinking",
    "working",
    "success",
    "failed",
    "review",
    "happy",
    "focused",
    "sleeping",
]


@dataclass(frozen=True, slots=True)
class PetIdentity:
    pet_id: str
    feishu_open_id: str
    employee_id: str = ""
    desktop_session_id: str = ""
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True, slots=True)
class PetSourceRef:
    employee_id: str = ""
    platform: str = ""
    conversation_id: str = ""
    message_id: str = ""
    router_trace_id: str = ""
    harness_task_id: str = ""
    hermes_job_id: str = ""
    obsidian_note_path: str = ""
    kb_doc_id: str = ""
    desktop_session_id: str = ""

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> PetSourceRef:
        data = value or {}
        allowed = {field.name for field in cls.__dataclass_fields__.values()}
        return cls(**{key: str(data.get(key) or "") for key in allowed})

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class PetState:
    pet_id: str
    user_id: str
    name: str = "散猫猫"
    species: str = "legacy-desktop-pet"
    asset_id: str = "sanmaomao"
    state: PetVisualState = "idle"
    emotion: str = "calm"
    scene: str = "desk"
    level: int = 1
    xp: int = 0
    coins: int = 0
    energy: int = 62
    streak_days: int = 0
    mood: str = "calm"
    focus_level: int = 0
    memory_affinity: int = 0
    work_rhythm: str = ""
    last_signal: dict[str, Any] = field(default_factory=dict)
    last_event_id: int = 0
    last_active_at: str = ""
    updated_at: str = ""

    def with_changes(self, **changes: Any) -> PetState:
        return replace(self, **changes)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> PetState:
        data = dict(value or {})
        allowed = {field.name for field in cls.__dataclass_fields__.values()}
        return cls(**{key: data[key] for key in allowed if key in data})


@dataclass(frozen=True, slots=True)
class PetEvent:
    pet_id: str
    user_id: str
    source: str
    event_type: str
    source_ref: PetSourceRef = field(default_factory=PetSourceRef)
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""


@dataclass(frozen=True, slots=True)
class StoredPetEvent(PetEvent):
    id: int = 0
    state_before: PetState | None = None
    state_after: PetState | None = None
