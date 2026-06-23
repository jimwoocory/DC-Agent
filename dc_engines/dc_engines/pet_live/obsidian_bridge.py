"""Bridge helpers for linking pet events to governed Obsidian/KB references."""

from __future__ import annotations

from .contracts import PetSourceRef, StoredPetEvent
from .event_bus import publish_pet_event
from .store import PetLiveStore


def source_ref_for_memory(
    *,
    employee_id: str = "",
    obsidian_note_path: str = "",
    kb_doc_id: str = "",
    harness_task_id: str = "",
) -> PetSourceRef:
    return PetSourceRef(
        employee_id=employee_id,
        obsidian_note_path=obsidian_note_path,
        kb_doc_id=kb_doc_id,
        harness_task_id=harness_task_id,
    )


def record_obsidian_note_linked(
    store: PetLiveStore,
    *,
    pet_id: str,
    user_id: str,
    obsidian_note_path: str,
    harness_task_id: str = "",
    memory_id: str = "",
    kb_doc_id: str = "",
    employee_id: str = "",
) -> StoredPetEvent:
    return publish_pet_event(
        store,
        pet_id=pet_id,
        user_id=user_id,
        source="obsidian",
        event_type="obsidian_note_linked",
        source_ref=source_ref_for_memory(
            employee_id=employee_id,
            obsidian_note_path=obsidian_note_path,
            kb_doc_id=kb_doc_id,
            harness_task_id=harness_task_id,
        ),
        payload={
            "employee_id": employee_id,
            "memory_id": memory_id,
            "obsidian_note_path": obsidian_note_path,
            "kb_doc_id": kb_doc_id,
        },
    )


def record_memory_promoted(
    store: PetLiveStore,
    *,
    pet_id: str,
    user_id: str,
    obsidian_note_path: str,
    kb_doc_id: str,
    harness_task_id: str = "",
    memory_id: str = "",
    employee_id: str = "",
) -> StoredPetEvent:
    return publish_pet_event(
        store,
        pet_id=pet_id,
        user_id=user_id,
        source="obsidian",
        event_type="memory_promoted",
        source_ref=source_ref_for_memory(
            employee_id=employee_id,
            obsidian_note_path=obsidian_note_path,
            kb_doc_id=kb_doc_id,
            harness_task_id=harness_task_id,
        ),
        payload={
            "employee_id": employee_id,
            "memory_id": memory_id,
            "obsidian_note_path": obsidian_note_path,
            "kb_doc_id": kb_doc_id,
        },
    )
