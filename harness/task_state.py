"""Queue state contracts for scarce-model jobs."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class QueueStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COOLDOWN = "cooldown"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AdmissionMode(str, Enum):
    RUN_NOW = "run_now"
    QUEUED = "queued"


@dataclass(slots=True)
class QueueJob:
    job_id: str
    primary_resource_key: str
    resource_keys: tuple[str, ...]
    status: QueueStatus
    payload: dict[str, Any] = field(default_factory=dict)
    requested_by: str | None = None
    session_id: str | None = None
    priority: int = 0
    enqueue_at: float = 0
    eta_at: float | None = None
    started_at: float | None = None
    completed_at: float | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    mode: AdmissionMode
    job: QueueJob
    queue_position: int = 0
    eta_at: float | None = None
    reason: str = ""
