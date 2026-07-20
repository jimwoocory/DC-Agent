from __future__ import annotations

from typing import Any

from dc_engines.org_permissions import (
    PermissionAssignment,
    build_principal_context,
    build_principal_context_from_employee,
)


def requester_meta_from_employee(
    emp: Any,
    *,
    admins_id: list[str] | tuple[str, ...] = (),
    permission_assignments: list[PermissionAssignment]
    | tuple[PermissionAssignment, ...]
    | None = None,
) -> dict[str, Any]:
    """Build Runtime Principal metadata from a known employee profile.

    Args:
        emp: Employee directory record supplying organization identity.
        admins_id: Legacy global DC administrator identifiers.
        permission_assignments: Persisted DC runtime permission assignments.

    Returns:
        Subject-bound requester metadata for events and Harness tasks.
    """

    preferences = getattr(emp, "preferences", None) or {}
    department_path = preferences.get("department_path") or []
    department_aliases = preferences.get("department_aliases") or []
    principal = build_principal_context_from_employee(
        emp,
        admins_id=admins_id,
        permission_assignments=permission_assignments,
    )
    return {
        "requester_open_id": emp.open_id,
        "requester_identity_source": "employee_directory",
        "requester_display_name": emp.display_name or "",
        "requester_department": emp.department or "",
        "requester_canonical_department": principal.department,
        "requester_business_department": preferences.get(
            "business_parent_department", ""
        ),
        "requester_department_path": department_path
        if isinstance(department_path, list)
        else [],
        "requester_canonical_department_path": list(principal.organization_path),
        "requester_department_aliases": department_aliases
        if isinstance(department_aliases, list)
        else [],
        "requester_role": emp.role or "",
        "requester_relation_type": emp.relation_type or "",
        "requester_managed_departments": list(principal.managed_departments),
        "requester_principal_type": principal.principal_type,
        "requester_dc_permissions": [
            assignment.to_record() for assignment in principal.permissions
        ],
        "requester_external_facts": principal.external_facts,
    }


async def requester_meta_from_event(context: Any, event: Any) -> dict[str, Any]:
    """Resolve or reuse the Runtime Principal for an inbound event.

    Args:
        context: Shared runtime context exposing identity and permission stores.
        event: Inbound event providing a stable sender identifier.

    Returns:
        Subject-bound requester metadata, or an empty mapping without a sender.
    """

    try:
        sender_id = str(event.get_sender_id() or "").strip()
    except Exception:  # noqa: BLE001
        sender_id = ""
    if not sender_id:
        return {}

    try:
        cached = event.get_extra("dc_runtime_principal", default=None)
    except Exception:  # noqa: BLE001
        cached = None
    if (
        isinstance(cached, dict)
        and str(cached.get("requester_open_id") or "").strip() == sender_id
    ):
        return dict(cached)

    admins_id: list[str] | tuple[str, ...] = ()
    try:
        config = (
            context.get_config()
            if callable(getattr(context, "get_config", None))
            else {}
        )
        if isinstance(config, dict):
            raw_admins = config.get("admins_id", [])
            if isinstance(raw_admins, list | tuple):
                admins_id = tuple(str(item) for item in raw_admins)
    except Exception:  # noqa: BLE001
        admins_id = ()

    permission_assignments: list[PermissionAssignment] = []
    permission_store = getattr(context, "dc_permission_store", None)
    if permission_store is not None:
        try:
            permission_assignments = await permission_store.list_assignments(sender_id)
        except Exception:  # noqa: BLE001
            permission_assignments = []

    emp = None
    emp_store = getattr(context, "employee_store", None)
    if emp_store is not None:
        try:
            emp = await emp_store.get_employee(sender_id)
        except Exception:  # noqa: BLE001
            emp = None

    if emp is None:
        principal = build_principal_context(
            subject_id=sender_id,
            admins_id=admins_id,
            permission_assignments=permission_assignments,
        )
        return {
            "requester_open_id": sender_id,
            "requester_identity_source": "event_sender",
            "requester_principal_type": principal.principal_type,
            "requester_dc_permissions": [
                assignment.to_record() for assignment in principal.permissions
            ],
            "requester_external_facts": principal.external_facts,
        }

    return requester_meta_from_employee(
        emp,
        admins_id=admins_id,
        permission_assignments=permission_assignments,
    )
