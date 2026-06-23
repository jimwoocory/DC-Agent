"""Live pet state core.

This package owns the pet identity, append-only event stream, and reducer-backed
state snapshot shared by Feishu cards, Router/Harness/Hermes integrations, and
the desktop client.
"""

from .contracts import PetEvent, PetIdentity, PetSourceRef, PetState, StoredPetEvent
from .event_bus import publish_pet_event
from .identity import get_or_create_identity
from .store import PetLiveStore

__all__ = [
    "PetEvent",
    "PetIdentity",
    "PetLiveStore",
    "PetSourceRef",
    "PetState",
    "StoredPetEvent",
    "get_or_create_identity",
    "publish_pet_event",
]
