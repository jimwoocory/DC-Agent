from __future__ import annotations

from typing import Any

from .contracts import PetEvent, PetSourceRef, StoredPetEvent
from .store import PetLiveStore


def publish_pet_event(
    store: PetLiveStore,
    *,
    pet_id: str,
    user_id: str,
    source: str,
    event_type: str,
    source_ref: PetSourceRef | dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
    created_at: str = "",
) -> StoredPetEvent:
    event = PetEvent(
        pet_id=pet_id,
        user_id=user_id,
        source=source,
        event_type=event_type,
        source_ref=(
            source_ref
            if isinstance(source_ref, PetSourceRef)
            else PetSourceRef.from_dict(source_ref)
        ),
        payload=payload or {},
        created_at=created_at,
    )
    return store.append_event(event)
