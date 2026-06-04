from __future__ import annotations

from typing import Any


async def requester_meta_from_event(context: Any, event: Any) -> dict[str, Any]:
    """Build a stable requester payload from the runtime event."""
    try:
        sender_id = str(event.get_sender_id() or "").strip()
    except Exception:  # noqa: BLE001
        sender_id = ""
    if not sender_id:
        return {}

    payload: dict[str, Any] = {"requester_open_id": sender_id}
    emp_store = getattr(context, "employee_store", None)
    if emp_store is None:
        return payload

    try:
        emp = await emp_store.get_employee(sender_id)
    except Exception:  # noqa: BLE001
        return payload
    if emp is None:
        return payload

    payload.update(
        {
            "requester_open_id": emp.open_id,
            "requester_display_name": emp.display_name or "",
            "requester_department": emp.department or "",
            "requester_role": emp.role or "",
        }
    )
    return payload
