from __future__ import annotations

import uuid

from .contracts import PetIdentity
from .store import PetLiveStore


def get_or_create_identity(
    store: PetLiveStore,
    *,
    feishu_open_id: str = "",
    employee_id: str | None = None,
    desktop_session_id: str | None = None,
) -> PetIdentity:
    """Return the stable pet identity for an employee.

    Employee ID is the primary identity. Feishu open_id is still accepted as a
    bootstrap fallback until the employee identity is known.
    """

    feishu_open_id = (feishu_open_id or "").strip()
    employee_id = (employee_id or "").strip()
    desktop_session_id = (desktop_session_id or "").strip()
    if not employee_id and not feishu_open_id:
        raise ValueError("employee_id or feishu_open_id is required")

    if employee_id:
        existing = store.get_identity_by_employee_id(employee_id)
        if existing is not None:
            return _patch_existing_identity(
                store,
                existing,
                feishu_open_id=feishu_open_id,
                employee_id=employee_id,
                desktop_session_id=desktop_session_id,
            )

    existing = (
        store.get_identity_by_feishu_open_id(feishu_open_id) if feishu_open_id else None
    )
    if existing is None:
        return store.create_identity(
            PetIdentity(
                pet_id=f"pet_{uuid.uuid4().hex}",
                feishu_open_id=feishu_open_id,
                employee_id=employee_id,
                desktop_session_id=desktop_session_id,
            )
        )

    return _patch_existing_identity(
        store,
        existing,
        feishu_open_id=feishu_open_id,
        employee_id=employee_id,
        desktop_session_id=desktop_session_id,
    )


def _patch_existing_identity(
    store: PetLiveStore,
    existing: PetIdentity,
    *,
    feishu_open_id: str,
    employee_id: str,
    desktop_session_id: str,
) -> PetIdentity:
    patch: dict[str, str] = {}
    if employee_id and existing.employee_id != employee_id:
        patch["employee_id"] = employee_id
    if feishu_open_id and existing.feishu_open_id != feishu_open_id:
        duplicate = store.get_identity_by_feishu_open_id(feishu_open_id)
        if duplicate is not None and duplicate.pet_id != existing.pet_id:
            if (
                duplicate.desktop_session_id
                and not existing.desktop_session_id
                and not desktop_session_id
            ):
                patch["desktop_session_id"] = duplicate.desktop_session_id
            store.merge_identity(
                source_pet_id=duplicate.pet_id,
                target_pet_id=existing.pet_id,
            )
        patch["feishu_open_id"] = feishu_open_id
    if desktop_session_id and existing.desktop_session_id != desktop_session_id:
        patch["desktop_session_id"] = desktop_session_id
    if patch:
        return store.update_identity(existing.pet_id, **patch)
    return existing
