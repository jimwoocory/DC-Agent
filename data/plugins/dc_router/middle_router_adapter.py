"""AstrBot Adapter for the pure middle Router Interface."""

from __future__ import annotations

import uuid
from typing import Any

from dc_router_core.middle_router import AgentDecision, CapabilityRoute, MiddleRouter

_ROUTER = MiddleRouter()


def route_capability_request(
    event: Any,
    *,
    source: str,
    capability_id: str,
    goal: str,
    confidence: float,
    action_force: str,
    trusted: bool,
    parameters: dict[str, Any] | None = None,
) -> CapabilityRoute:
    """Route and annotate one structured capability request.

    Args:
        event: Current AstrBot event carrying Runtime Principal metadata.
        source: Agent, menu, or card Adapter source.
        capability_id: Stable catalog capability identifier.
        goal: User objective without implementation names.
        confidence: Agent confidence or one for deterministic Adapters.
        action_force: Navigate, prepare, or execute.
        trusted: Whether a deterministic source was verified by its Adapter.
        parameters: Sanitized structured parameters for downstream execution.

    Returns:
        Deterministic middle Router decision.
    """

    subject_id = ""
    try:
        principal = event.get_extra("dc_runtime_principal", default={}) or {}
        if isinstance(principal, dict):
            subject_id = str(
                principal.get("subject_id")
                or principal.get("open_id")
                or principal.get("user_id")
                or ""
            ).strip()
    except Exception:  # noqa: BLE001
        pass
    if not subject_id:
        try:
            subject_id = str(event.get_sender_id() or "").strip()
        except Exception:  # noqa: BLE001
            pass

    sanitized_goal = str(goal or "")[:4000]
    sanitized_parameters = dict(parameters or {})
    route = _ROUTER.route(
        AgentDecision(
            request_id=uuid.uuid4().hex,
            source=source,  # type: ignore[arg-type]
            capability_id=capability_id,
            goal=sanitized_goal,
            confidence=float(confidence),
            action_force=action_force,  # type: ignore[arg-type]
            trusted=bool(trusted),
            parameters=sanitized_parameters,
            subject_id=subject_id,
        )
    )
    try:
        event.set_extra("dc_middle_router_request_id", route.request_id)
        event.set_extra("dc_middle_router_source", route.source)
        event.set_extra("dc_middle_router_capability", route.capability_id)
        event.set_extra("dc_middle_router_goal", sanitized_goal)
        event.set_extra("dc_middle_router_parameters", sanitized_parameters)
        event.set_extra("dc_middle_router_allowed", route.allowed)
        event.set_extra("dc_middle_router_target", route.target)
        event.set_extra("dc_middle_router_executor", route.executor)
        event.set_extra("dc_middle_router_depth", route.depth)
        event.set_extra("dc_middle_router_reason", route.reason)
    except Exception:  # noqa: BLE001
        pass
    return route


__all__ = ["route_capability_request"]
