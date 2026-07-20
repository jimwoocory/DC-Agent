"""Contract tests for the deterministic middle Router Deep Module."""

from __future__ import annotations

from dc_router_core.middle_router import AgentDecision, MiddleRouter


def _decision(**overrides) -> AgentDecision:
    values = {
        "request_id": "req-1",
        "source": "agent",
        "capability_id": "workspace.image",
        "goal": "为新品活动准备一张横版主视觉",
        "confidence": 0.9,
        "action_force": "prepare",
    }
    values.update(overrides)
    return AgentDecision(**values)


def test_agent_workspace_request_maps_to_h5_without_accepting_executor_input() -> None:
    route = MiddleRouter().route(_decision())

    assert route.allowed is True
    assert route.target == "h5_workbench"
    assert route.executor == "assistant_h5"
    assert route.task_type == "image"
    assert route.depth == "workspace"


def test_unknown_capability_is_denied() -> None:
    route = MiddleRouter().route(
        _decision(capability_id="provider.codex/arbitrary-shell")
    )

    assert route.allowed is False
    assert route.executor == "none"
    assert route.reason == "unknown_capability"


def test_untrusted_card_cannot_execute() -> None:
    route = MiddleRouter().route(
        _decision(
            source="card",
            capability_id="execute.image",
            action_force="execute",
            confidence=1.0,
            trusted=False,
        )
    )

    assert route.allowed is False
    assert route.reason == "untrusted_adapter"


def test_trusted_card_media_request_maps_to_harness_executor() -> None:
    route = MiddleRouter().route(
        _decision(
            source="card",
            capability_id="execute.video",
            action_force="execute",
            confidence=1.0,
            trusted=True,
        )
    )

    assert route.allowed is True
    assert route.target == "harness_executor"
    assert route.executor == "media_route"
    assert route.depth == "governed"


def test_main_agent_confirmed_media_request_maps_to_harness_executor() -> None:
    route = MiddleRouter().route(
        _decision(
            source="agent",
            capability_id="execute.image",
            action_force="execute",
            confidence=0.98,
            goal="原创挪威红、蓝、白配色的北欧冰雪足球怪兽海报",
        )
    )

    assert route.allowed is True
    assert route.target == "harness_executor"
    assert route.executor == "media_route"
    assert route.depth == "governed"


def test_delegate_requires_explicit_execution_and_confidence_floor() -> None:
    router = MiddleRouter()

    prepare_route = router.route(
        _decision(capability_id="delegate.research", action_force="prepare")
    )
    low_confidence_route = router.route(
        _decision(
            capability_id="delegate.research",
            action_force="execute",
            confidence=0.5,
        )
    )
    allowed_route = router.route(
        _decision(
            capability_id="delegate.research",
            action_force="execute",
            confidence=0.9,
        )
    )

    assert prepare_route.reason == "explicit_execute_required"
    assert low_confidence_route.reason == "low_confidence"
    assert allowed_route.allowed is True
    assert allowed_route.executor == "transfer_to_research_agent"


def test_deterministic_menu_requires_trusted_adapter() -> None:
    denied = MiddleRouter().route(
        _decision(
            source="menu",
            capability_id="workspace.copy",
            goal="写文案/方案",
            confidence=1.0,
            action_force="navigate",
            trusted=False,
        )
    )
    allowed = MiddleRouter().route(
        _decision(
            source="menu",
            capability_id="workspace.copy",
            goal="写文案/方案",
            confidence=1.0,
            action_force="navigate",
            trusted=True,
        )
    )

    assert denied.reason == "untrusted_adapter"
    assert allowed.allowed is True
