from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

CaseStatus = Literal[
    "initiated",
    "drafting",
    "reviewing",
    "escalated",
    "archived",
    "cancelled",
]

CASE_TERMINAL_STATUSES: set[CaseStatus] = {"archived", "cancelled"}

CASE_ACTIVE_STATUSES: set[CaseStatus] = {
    "initiated",
    "drafting",
    "reviewing",
    "escalated",
}


@dataclass(slots=True)
class Case:
    case_id: str
    name: str
    status: CaseStatus
    client_name: str | None
    platform_id: str
    session_id: str
    task_ids: list[str]
    deliverables: list[dict[str, Any]]
    roles: dict[str, str]
    version: int
    created_at: str
    updated_at: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CaseEvent:
    event_id: str
    case_id: str
    event_type: str
    payload: dict[str, Any]
    created_at: str


@dataclass(slots=True)
class CaseDeliverable:
    """Convenience helper for building deliverable dicts.

    Stored inside ``Case.deliverables`` as a plain dict so it round-trips
    through SQLite/JSON cleanly.
    """

    kind: str
    path: str
    version: int | None = None
    created_at: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "kind": self.kind,
            "path": self.path,
            "created_at": self.created_at,
        }
        if self.version is not None:
            data["version"] = self.version
        if self.extra:
            data.update(self.extra)
        return data
