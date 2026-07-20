from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

HarnessTaskStatus = Literal[
    "pending",
    "in_progress",
    "blocked",
    "review_required",
    "completed",
    "cancelled",
    "failed",
]

HarnessReviewDecision = Literal[
    "approved",
    "rejected",
]

HarnessExecutionStatus = Literal[
    "running",
    "succeeded",
    "failed",
    "cancelled",
]

HarnessTaskRelation = Literal[
    "root",
    "revision",
]

HarnessSettlementOutcome = Literal[
    "completed",
    "review_required",
    "blocked",
    "failed",
    "cancelled",
]

HarnessSettlementStatus = Literal["pending", "applied"]

HarnessDeliveryMode = Literal["confirmed", "runtime_owned", "not_required"]

HarnessSessionDecisionState = Literal[
    "pending",
    "opening_new",
    "new_conversation",
    "continue_current",
    "superseded_continue",
]

HarnessSessionDecisionSource = Literal[
    "",
    "card_click",
    "implicit_message",
    "superseded",
]

HarnessSessionCardPatchState = Literal[
    "not_sent",
    "pending",
    "synced",
    "failed",
]

HARNESS_TERMINAL_STATUSES: set[HarnessTaskStatus] = {
    "completed",
    "cancelled",
    "failed",
}


@dataclass(slots=True)
class HarnessTaskCreateRequest:
    title: str
    conversation_id: str
    platform_id: str
    session_id: str
    domain: str = "general"
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class HarnessTask:
    task_id: str
    conversation_id: str
    platform_id: str
    session_id: str
    title: str
    domain: str
    status: HarnessTaskStatus
    payload: dict[str, Any]
    result: dict[str, Any]
    created_at: str
    updated_at: str


@dataclass(slots=True)
class HarnessTaskEvent:
    event_id: str
    task_id: str
    event_type: str
    payload: dict[str, Any]
    created_at: str


@dataclass(slots=True)
class HarnessTaskReview:
    review_id: str
    task_id: str
    reviewer_id: str
    decision: HarnessReviewDecision
    note: str
    created_at: str


@dataclass(slots=True)
class HarnessWorkContext:
    context_id: str
    scope_key: str
    platform_id: str
    conversation_id: str
    subject_ref: str
    last_session_id: str
    created_at: str
    updated_at: str


@dataclass(slots=True)
class HarnessMessageReference:
    message_ref_id: str
    context_id: str
    task_id: str | None
    platform_message_id: str
    session_id: str
    direction: str
    content_digest: str
    created_at: str


@dataclass(slots=True)
class HarnessTaskLink:
    task_id: str
    context_id: str
    parent_task_id: str | None
    relation_type: HarnessTaskRelation
    message_ref_id: str | None
    created_at: str


@dataclass(slots=True)
class HarnessExecution:
    execution_id: str
    task_id: str
    executor_kind: str
    capability: str
    status: HarnessExecutionStatus
    idempotency_key: str
    request_digest: str
    metadata: dict[str, Any]
    error_summary: str
    started_at: str
    finished_at: str | None


@dataclass(slots=True)
class HarnessArtifact:
    artifact_id: str
    context_id: str
    task_id: str
    execution_id: str
    artifact_kind: str
    uri: str
    mime_type: str
    root_artifact_id: str
    parent_artifact_id: str | None
    version: int
    idempotency_key: str
    metadata: dict[str, Any]
    created_at: str


@dataclass(slots=True, frozen=True)
class HarnessArtifactSpec:
    """Artifact proposed by an executor settlement.

    Attributes:
        context_id: Owning Harness work context.
        artifact_kind: Artifact type such as image or video.
        uri: Durable output reference.
        mime_type: Artifact MIME type.
        metadata: Non-sensitive Artifact metadata.
        parent_artifact_id: Optional source Artifact for a revision.
    """

    context_id: str
    artifact_kind: str
    uri: str
    mime_type: str
    metadata: dict[str, Any] = field(default_factory=dict)
    parent_artifact_id: str | None = None


@dataclass(slots=True, frozen=True)
class HarnessDeliveryReceipt:
    """Minimal evidence that an executor result entered a delivery path.

    Attributes:
        mode: Confirmed, runtime-owned, or not-required delivery.
        reference_digest: Digest or non-sensitive reference for the delivery.
    """

    mode: HarnessDeliveryMode
    reference_digest: str


@dataclass(slots=True)
class HarnessExecutionSettlement:
    settlement_id: str
    execution_id: str
    task_id: str
    outcome: HarnessSettlementOutcome
    status: HarnessSettlementStatus
    idempotency_key: str
    request_digest: str
    result: dict[str, Any]
    delivery: dict[str, Any]
    artifact_id: str | None
    created_at: str
    applied_at: str | None


@dataclass(slots=True)
class HarnessSessionDecision:
    """Durable routing decision after one assistant task completes.

    Attributes:
        decision_id: Stable idempotency key embedded in the Feishu card.
        task_id: Completed assistant task or deterministic result identifier.
        unified_msg_origin: AstrBot session whose conversation is being routed.
        platform_id: AstrBot platform instance identifier.
        chat_id: Feishu destination used to deliver the decision card.
        source_conversation_id: Conversation active when the result completed.
        target_conversation_id: Conversation selected by the final decision.
        card_message_id: Feishu message containing the decision card.
        state: Current state in the session-decision state machine.
        operator_id: User that made or implicitly triggered the decision.
        decision_source: Card click, implicit message, or supersession.
        card_patch_state: Synchronization state of the visible Feishu card.
        created_at: UTC creation timestamp.
        decided_at: UTC terminal-decision timestamp when available.
        updated_at: UTC timestamp of the latest state change.
    """

    decision_id: str
    task_id: str
    unified_msg_origin: str
    platform_id: str
    chat_id: str
    source_conversation_id: str
    target_conversation_id: str
    card_message_id: str
    state: HarnessSessionDecisionState
    operator_id: str
    decision_source: HarnessSessionDecisionSource
    card_patch_state: HarnessSessionCardPatchState
    created_at: str
    decided_at: str | None
    updated_at: str
