"""Deterministic middle Router for structured capability requests.

The main Agent may understand intent, but it never selects an implementation.
This Module owns the stable capability catalog and maps approved requests to one
bounded executor Interface. Menu and card Adapters use the same entry point.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

CapabilitySource = Literal["agent", "menu", "card"]
ActionForce = Literal["navigate", "prepare", "execute"]
ExecutionDepth = Literal["direct", "workspace", "delegated", "governed"]


@dataclass(frozen=True, slots=True)
class AgentDecision:
    """A structured request presented to the middle Router Interface.

    Attributes:
        request_id: Adapter-generated identifier used for tracing.
        source: Originating Adapter; one of agent, menu, or trusted card.
        capability_id: Stable capability identifier from the catalog.
        goal: User-visible objective without hidden implementation names.
        confidence: Agent confidence from zero to one. Deterministic Adapters use one.
        action_force: Whether the request navigates, prepares, or executes work.
        trusted: Whether a deterministic menu/card Adapter verified its source.
        parameters: Sanitized task parameters. Executor names are not accepted here.
        subject_id: Runtime Principal identifier used for audit correlation.
        schema_version: Version of this Interface payload.
    """

    request_id: str
    source: CapabilitySource
    capability_id: str
    goal: str
    confidence: float
    action_force: ActionForce
    trusted: bool = False
    parameters: dict[str, Any] = field(default_factory=dict)
    subject_id: str = ""
    schema_version: str = "1"


@dataclass(frozen=True, slots=True)
class CapabilityDefinition:
    """One deterministic mapping in the middle Router catalog.

    Attributes:
        capability_id: Stable public identifier selected by an Adapter or Agent.
        target: Architectural target Interface, not an arbitrary model/tool name.
        executor: Deterministic implementation selected by the Router.
        depth: Execution Depth used for observability and governance.
        allowed_sources: Adapters allowed to request this capability.
        minimum_confidence: Required Agent confidence; deterministic sources use one.
        requires_explicit_execute: Whether action_force must be execute.
        task_type: Optional H5 workbench task category.
    """

    capability_id: str
    target: str
    executor: str
    depth: ExecutionDepth
    allowed_sources: tuple[CapabilitySource, ...]
    minimum_confidence: float = 0.0
    requires_explicit_execute: bool = False
    task_type: str = ""


@dataclass(frozen=True, slots=True)
class CapabilityRoute:
    """Deterministic output returned by the middle Router.

    Attributes:
        request_id: Correlation identifier copied from the request.
        allowed: Whether an executor Adapter may continue.
        capability_id: Validated capability identifier.
        target: Target Interface selected from the catalog.
        executor: Concrete bounded executor selected from the catalog.
        depth: Selected execution Depth.
        task_type: Optional H5 task type.
        reason: Stable allow/deny reason code.
        source: Validated request source.
    """

    request_id: str
    allowed: bool
    capability_id: str
    target: str
    executor: str
    depth: ExecutionDepth
    task_type: str
    reason: str
    source: str


_WORKSPACE_SOURCES: tuple[CapabilitySource, ...] = ("agent", "menu", "card")
_CAPABILITIES = (
    CapabilityDefinition(
        "ui.workbench",
        "local_ui",
        "assistant_workbench",
        "direct",
        ("menu", "card"),
    ),
    *(
        CapabilityDefinition(
            f"workspace.{task_type}",
            "h5_workbench",
            "assistant_h5",
            "workspace",
            _WORKSPACE_SOURCES,
            minimum_confidence=0.65,
            task_type=task_type,
        )
        for task_type in (
            "copy",
            "image",
            "video",
            "research",
            "file",
            "quotation",
            "ai_cdr",
            "codex",
        )
    ),
    CapabilityDefinition(
        "delegate.research",
        "astrbot_subagent",
        "transfer_to_research_agent",
        "delegated",
        ("agent",),
        minimum_confidence=0.72,
        requires_explicit_execute=True,
    ),
    CapabilityDefinition(
        "delegate.office",
        "astrbot_subagent",
        "transfer_to_office_agent",
        "delegated",
        ("agent",),
        minimum_confidence=0.72,
        requires_explicit_execute=True,
    ),
    CapabilityDefinition(
        "delegate.analysis",
        "astrbot_subagent",
        "transfer_to_analysis_agent",
        "delegated",
        ("agent",),
        minimum_confidence=0.72,
        requires_explicit_execute=True,
    ),
    *(
        CapabilityDefinition(
            f"execute.{task_type}",
            "main_agent",
            "luna_main_agent",
            "direct",
            ("card",),
            requires_explicit_execute=True,
        )
        for task_type in ("copy", "research", "file", "quotation", "ai_cdr")
    ),
    *(
        CapabilityDefinition(
            f"execute.{task_type}",
            "harness_executor",
            "media_route",
            "governed",
            ("agent", "card"),
            minimum_confidence=0.72,
            requires_explicit_execute=True,
        )
        for task_type in ("image", "video")
    ),
    CapabilityDefinition(
        "execute.file.translate",
        "deterministic_tool",
        "office_translation",
        "governed",
        ("card",),
        requires_explicit_execute=True,
    ),
    CapabilityDefinition(
        "execute.codex",
        "advanced_executor",
        "codex_cli",
        "governed",
        ("card",),
        requires_explicit_execute=True,
    ),
)
_CAPABILITY_BY_ID = {item.capability_id: item for item in _CAPABILITIES}


def list_middle_router_capabilities() -> tuple[CapabilityDefinition, ...]:
    """Return the immutable middle Router capability catalog.

    Returns:
        Capability definitions shared by Agent, menu, and card Adapters.
    """

    return _CAPABILITIES


class MiddleRouter:
    """Validate structured requests and select one deterministic executor."""

    def route(self, decision: AgentDecision) -> CapabilityRoute:
        """Route one structured decision without performing side effects.

        Args:
            decision: Candidate request from the main Agent or deterministic Adapter.

        Returns:
            An allow/deny route containing only catalog-owned implementation data.
        """

        capability_id = str(decision.capability_id or "").strip()
        definition = _CAPABILITY_BY_ID.get(capability_id)
        reason = "authorized"
        if decision.schema_version != "1":
            reason = "unsupported_schema_version"
        elif decision.source not in {"agent", "menu", "card"}:
            reason = "invalid_source"
        elif definition is None:
            reason = "unknown_capability"
        elif decision.source not in definition.allowed_sources:
            reason = "source_not_allowed"
        elif decision.source in {"menu", "card"} and not decision.trusted:
            reason = "untrusted_adapter"
        elif decision.source == "agent" and not str(decision.goal or "").strip():
            reason = "missing_goal"
        elif not 0.0 <= float(decision.confidence) <= 1.0:
            reason = "invalid_confidence"
        elif float(decision.confidence) < definition.minimum_confidence:
            reason = "low_confidence"
        elif (
            definition.requires_explicit_execute and decision.action_force != "execute"
        ):
            reason = "explicit_execute_required"

        allowed = reason == "authorized" and definition is not None
        return CapabilityRoute(
            request_id=str(decision.request_id or ""),
            allowed=allowed,
            capability_id=capability_id,
            target=definition.target if allowed and definition is not None else "none",
            executor=(
                definition.executor if allowed and definition is not None else "none"
            ),
            depth=definition.depth if allowed and definition is not None else "direct",
            task_type=(
                definition.task_type if allowed and definition is not None else ""
            ),
            reason=reason,
            source=str(decision.source or ""),
        )


__all__ = [
    "ActionForce",
    "AgentDecision",
    "CapabilityDefinition",
    "CapabilityRoute",
    "CapabilitySource",
    "ExecutionDepth",
    "MiddleRouter",
    "list_middle_router_capabilities",
]
