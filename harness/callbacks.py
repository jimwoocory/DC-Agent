"""Callback contracts for queued and deep tasks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class TaskCallbackPayload:
    job_id: str
    session_id: str | None
    status: str
    result: dict[str, Any] | None = None
    error: str | None = None


class TaskCallbackSink(Protocol):
    async def send(self, payload: TaskCallbackPayload) -> None:
        """Deliver a queued task update to AstrBot or Dashboard."""
