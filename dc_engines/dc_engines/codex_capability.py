"""Deterministic authorization Interface for Codex Advanced Executor calls."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass

CODEX_DEVELOPMENT_MODEL = "gpt-5.6-sol"
CODEX_DEVELOPMENT_MIN_REASONING = "high"
CODEX_DEVELOPMENT_DEFAULT_REASONING = "max"
_REASONING_RANK = {
    "none": 0,
    "minimal": 1,
    "low": 2,
    "medium": 3,
    "high": 4,
    "xhigh": 5,
    "max": 6,
}


@dataclass(frozen=True, slots=True)
class CodexCapabilityDefinition:
    """One user-visible capability behind the Advanced Executor Interface.

    Attributes:
        capability: Stable capability identifier used for authorization.
        name: Human-readable capability name.
        description: Concise description of the bounded work.
        authorized_by: Authorities allowed to request the capability.
        entrypoints: Truthful user or controller entrypoints.
        execution_mode: Whether execution is interactive or controller-triggered.
        owns_schedule: Fixed false value documenting the runtime boundary.
    """

    capability: str
    name: str
    description: str
    authorized_by: tuple[str, ...]
    entrypoints: tuple[str, ...]
    execution_mode: str
    owns_schedule: bool = False


_CAPABILITIES = (
    CodexCapabilityDefinition(
        capability="deep_reasoning",
        name="深度分析与复核",
        description="处理复杂分析、方案推演和跨模型复核，不修改项目文件。",
        authorized_by=("user",),
        entrypoints=(
            "飞书消息：Codex 高级工具",
            "消息前缀：#codex工具",
        ),
        execution_mode="interactive",
    ),
    CodexCapabilityDefinition(
        capability="image_generation",
        name="图片生成与编辑",
        description="根据明确的用户需求生成或编辑图片。",
        authorized_by=("user",),
        entrypoints=("飞书：生成图片",),
        execution_mode="interactive",
    ),
    CodexCapabilityDefinition(
        capability="incident_diagnosis",
        name="系统故障诊断",
        description="在确定性探针发现故障后生成诊断建议，不接管恢复调度。",
        authorized_by=("deterministic_controller",),
        entrypoints=("dc-watchdog",),
        execution_mode="controller_triggered",
    ),
    CodexCapabilityDefinition(
        capability="project_engineering",
        name="项目开发与工程修改",
        description=(
            "仅限本机操作员显式调用；固定使用 GPT-5.6-Sol，推理强度不得低于 high。"
        ),
        authorized_by=("local_operator",),
        entrypoints=("本机 Codex CLI 受控开发 Adapter",),
        execution_mode="local_workspace_write",
    ),
)
_CAPABILITY_BY_ID = {item.capability: item for item in _CAPABILITIES}


@dataclass(frozen=True, slots=True)
class CodexAuthorization:
    """Machine-readable decision returned by the authorization Interface.

    Attributes:
        allowed: Whether the Adapter may invoke Codex.
        capability: Requested Codex capability.
        authorized_by: Authority that requested the invocation.
        owns_schedule: Whether Codex would own scheduling for the request.
        role: Fixed architectural role for Codex.
        reason: Stable reason code for the decision.
    """

    allowed: bool
    capability: str
    authorized_by: str
    owns_schedule: bool
    role: str
    reason: str


@dataclass(frozen=True, slots=True)
class CodexDevelopmentPolicyDecision:
    """Decision for the mandatory Codex development runtime policy.

    Attributes:
        allowed: Whether the requested development runtime is permitted.
        model: Requested Codex model slug.
        reasoning_effort: Requested reasoning level.
        required_model: Model mandated for development work.
        minimum_reasoning_effort: Lowest permitted reasoning level.
        default_reasoning_effort: Preferred reasoning level for development.
        reason: Stable reason code for the decision.
    """

    allowed: bool
    model: str
    reasoning_effort: str
    required_model: str
    minimum_reasoning_effort: str
    default_reasoning_effort: str
    reason: str


def list_codex_capabilities() -> tuple[CodexCapabilityDefinition, ...]:
    """Return the deterministic Advanced Executor capability catalog.

    Returns:
        Immutable capability definitions shared by runtime Adapters and views.
    """
    return _CAPABILITIES


def validate_codex_development_runtime(
    *,
    model: str,
    reasoning_effort: str,
) -> CodexDevelopmentPolicyDecision:
    """Validate the model and reasoning floor for project development.

    Args:
        model: Requested Codex CLI model slug.
        reasoning_effort: Requested Codex reasoning level.

    Returns:
        A deterministic decision. Development requires GPT-5.6-Sol and a
        reasoning level ranked at least ``high``.
    """
    normalized_model = str(model or "").strip().lower()
    normalized_effort = str(reasoning_effort or "").strip().lower()
    if normalized_model != CODEX_DEVELOPMENT_MODEL:
        reason = "development_model_required"
    elif normalized_effort not in _REASONING_RANK:
        reason = "unknown_reasoning_effort"
    elif (
        _REASONING_RANK[normalized_effort]
        < _REASONING_RANK[CODEX_DEVELOPMENT_MIN_REASONING]
    ):
        reason = "development_reasoning_too_low"
    else:
        reason = "authorized"
    return CodexDevelopmentPolicyDecision(
        allowed=reason == "authorized",
        model=normalized_model,
        reasoning_effort=normalized_effort,
        required_model=CODEX_DEVELOPMENT_MODEL,
        minimum_reasoning_effort=CODEX_DEVELOPMENT_MIN_REASONING,
        default_reasoning_effort=CODEX_DEVELOPMENT_DEFAULT_REASONING,
        reason=reason,
    )


def authorize_codex(
    capability: str,
    *,
    authorized_by: str,
    owns_schedule: bool = False,
) -> CodexAuthorization:
    """Authorize one Codex capability without inspecting runtime credentials.

    Args:
        capability: Capability requested by the calling Adapter.
        authorized_by: Authority responsible for the request.
        owns_schedule: Whether Codex would own recurring or deferred scheduling.

    Returns:
        A deterministic authorization decision. Scheduler ownership is always
        denied because Codex is an Advanced Executor, not a controller.
    """
    if owns_schedule:
        return CodexAuthorization(
            allowed=False,
            capability=capability,
            authorized_by=authorized_by,
            owns_schedule=True,
            role="advanced_executor",
            reason="scheduler_ownership_denied",
        )

    definition = _CAPABILITY_BY_ID.get(capability)
    allowed = definition is not None and authorized_by in definition.authorized_by
    return CodexAuthorization(
        allowed=allowed,
        capability=capability,
        authorized_by=authorized_by,
        owns_schedule=False,
        role="advanced_executor",
        reason="authorized" if allowed else "authority_not_allowed",
    )


def main(argv: list[str] | None = None) -> int:
    """Expose the authorization Interface to shell-based Adapters.

    Args:
        argv: Optional command-line arguments. Defaults to ``sys.argv``.

    Returns:
        Zero when authorized, otherwise two.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    authorize_parser = subparsers.add_parser("authorize")
    authorize_parser.add_argument("--capability", required=True)
    authorize_parser.add_argument("--authorized-by", required=True)
    authorize_parser.add_argument("--owns-schedule", action="store_true")
    subparsers.add_parser("catalog")
    development_parser = subparsers.add_parser("validate-development")
    development_parser.add_argument("--model", required=True)
    development_parser.add_argument("--reasoning-effort", required=True)
    args = parser.parse_args(argv)

    if args.command == "catalog":
        print(
            json.dumps(
                [asdict(item) for item in list_codex_capabilities()],
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    if args.command == "validate-development":
        development = validate_codex_development_runtime(
            model=args.model,
            reasoning_effort=args.reasoning_effort,
        )
        print(json.dumps(asdict(development), ensure_ascii=False, sort_keys=True))
        return 0 if development.allowed else 2

    decision = authorize_codex(
        args.capability,
        authorized_by=args.authorized_by,
        owns_schedule=args.owns_schedule,
    )
    print(json.dumps(asdict(decision), ensure_ascii=False, sort_keys=True))
    return 0 if decision.allowed else 2


if __name__ == "__main__":
    raise SystemExit(main())
