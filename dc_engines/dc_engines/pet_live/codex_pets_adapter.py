"""Adapter payloads for the vendored codex-pets presentation layer.

Pet Live keeps the business state machine. codex-pets owns presentation states
such as running, waving, and jumping. This module is the explicit boundary
between those two models.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

from .contracts import PetState, PetVisualState

CodexPetsStateId = Literal[
    "idle",
    "running-right",
    "running-left",
    "waving",
    "jumping",
    "failed",
    "waiting",
    "running",
    "review",
]

CODEX_PETS_VENDOR_COMMIT = "5615902ec508bbd0feb8c6baeab21c951c940824"
CODEX_PETS_ATLAS = {
    "cell": {"width": 192, "height": 208},
    "columns": 8,
    "rows": 9,
}

PET_LIVE_TO_CODEX_PETS_STATE: dict[PetVisualState, CodexPetsStateId] = {
    "idle": "idle",
    "waiting": "waiting",
    "thinking": "review",
    "working": "running",
    "success": "waving",
    "failed": "failed",
    "review": "review",
    "happy": "waving",
    "focused": "idle",
    "sleeping": "waiting",
}

STATE_REASON: dict[PetVisualState, str] = {
    "idle": "No active work event is driving the pet.",
    "waiting": "The assistant is waiting for user input or an upstream reply.",
    "thinking": "Router or assistant reasoning is in progress.",
    "working": "Harness or Hermes is actively executing work.",
    "success": "The latest task completed successfully.",
    "failed": "The latest task failed or needs recovery.",
    "review": "The latest result requires human review.",
    "happy": "A positive interaction or reward event was recorded.",
    "focused": "Approved memory or knowledge context was injected.",
    "sleeping": "The pet is inactive but still bound to the same identity.",
}


@dataclass(frozen=True, slots=True)
class CodexPetsSceneState:
    pet_id: str
    asset_id: str
    live_state: PetVisualState
    codex_state: CodexPetsStateId
    display_name: str
    scene: str
    emotion: str
    level: int
    xp: int
    energy: int
    coins: int
    atlas: dict
    reason: str
    vendor_commit: str = CODEX_PETS_VENDOR_COMMIT

    def to_dict(self) -> dict:
        return asdict(self)


def codex_pets_state_for(live_state: PetVisualState) -> CodexPetsStateId:
    return PET_LIVE_TO_CODEX_PETS_STATE.get(live_state, "idle")


def codex_pets_scene_state(pet_state: PetState) -> CodexPetsSceneState:
    return CodexPetsSceneState(
        pet_id=pet_state.pet_id,
        asset_id=pet_state.asset_id,
        live_state=pet_state.state,
        codex_state=codex_pets_state_for(pet_state.state),
        display_name=pet_state.name,
        scene=pet_state.scene,
        emotion=pet_state.emotion,
        level=pet_state.level,
        xp=pet_state.xp,
        energy=pet_state.energy,
        coins=pet_state.coins,
        atlas=CODEX_PETS_ATLAS,
        reason=STATE_REASON.get(pet_state.state, STATE_REASON["idle"]),
    )


def codex_pets_payload(pet_state: PetState) -> dict:
    """Return JSON-safe codex-pets presentation data for API responses."""

    return codex_pets_scene_state(pet_state).to_dict()
