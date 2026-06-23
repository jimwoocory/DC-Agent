from __future__ import annotations

from typing import Any

from .contracts import PetIdentity, PetState, StoredPetEvent
from .event_bus import publish_pet_event
from .identity import get_or_create_identity
from .store import PetLiveStore


class PetLiveService:
    def __init__(self, store: PetLiveStore) -> None:
        self.store = store

    def get_or_create_identity(
        self,
        *,
        feishu_open_id: str = "",
        employee_id: str | None = None,
        desktop_session_id: str | None = None,
    ) -> PetIdentity:
        return get_or_create_identity(
            self.store,
            feishu_open_id=feishu_open_id,
            employee_id=employee_id,
            desktop_session_id=desktop_session_id,
        )

    def get_pet_state(self, pet_id: str) -> PetState | None:
        return self.store.get_pet_state(pet_id)

    def list_events_after(
        self,
        pet_id: str,
        *,
        after_id: int = 0,
        limit: int = 100,
    ) -> list[StoredPetEvent]:
        return self.store.list_events_after(pet_id, after_id=after_id, limit=limit)

    def record_event(
        self,
        *,
        pet_id: str,
        user_id: str,
        source: str,
        event_type: str,
        source_ref: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        created_at: str = "",
    ) -> StoredPetEvent:
        return publish_pet_event(
            self.store,
            pet_id=pet_id,
            user_id=user_id,
            source=source,
            event_type=event_type,
            source_ref=source_ref,
            payload=payload,
            created_at=created_at,
        )
