from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class MemoryLayer(StrEnum):
    SHORT_TERM = "short_term"
    MEDIUM_TERM = "medium_term"
    LONG_TERM = "long_term"


class MemoryWatermarkAction(StrEnum):
    KEEP = "keep"
    COMPRESS_OR_TRIM = "compress_or_trim"
    CLEANUP_AND_PROMOTE = "cleanup_merge_and_promote_candidates"
    ARCHIVE_TO_NAS_AND_DISTILL = "archive_to_nas_and_distill"


class LarkChatScenario(StrEnum):
    CASUAL = "casual"
    BUSINESS = "business"
    COMPLEX = "complex"


@dataclass(frozen=True, slots=True)
class MemoryWatermarkPolicy:
    short_casual_tokens: int = 12_000
    short_business_tokens: int = 24_000
    short_complex_tokens: int = 32_000
    medium_max_items_per_employee: int = 50
    medium_max_tokens_per_employee: int = 50_000
    short_warning_ratio: float = 0.8

    def short_limit_for(self, scenario: LarkChatScenario | str) -> int:
        scenario_value = str(scenario or LarkChatScenario.CASUAL)
        if scenario_value == LarkChatScenario.COMPLEX:
            return self.short_complex_tokens
        if scenario_value == LarkChatScenario.BUSINESS:
            return self.short_business_tokens
        return self.short_casual_tokens


@dataclass(frozen=True, slots=True)
class MemoryWatermarkDecision:
    layer: MemoryLayer
    action: MemoryWatermarkAction
    limit: int
    current: int
    reason: str
    metadata: dict[str, str | int | float] = field(default_factory=dict)

    @property
    def exceeded(self) -> bool:
        return self.current > self.limit if self.limit > 0 else False


def evaluate_short_term_watermark(
    *,
    current_tokens: int,
    scenario: LarkChatScenario | str = LarkChatScenario.CASUAL,
    policy: MemoryWatermarkPolicy | None = None,
) -> MemoryWatermarkDecision:
    active_policy = policy or MemoryWatermarkPolicy()
    limit = active_policy.short_limit_for(scenario)
    action = (
        MemoryWatermarkAction.COMPRESS_OR_TRIM
        if current_tokens > limit
        else MemoryWatermarkAction.KEEP
    )
    return MemoryWatermarkDecision(
        layer=MemoryLayer.SHORT_TERM,
        action=action,
        limit=limit,
        current=current_tokens,
        reason=(
            "short_term_context_exceeds_lark_budget"
            if action == MemoryWatermarkAction.COMPRESS_OR_TRIM
            else "short_term_context_within_lark_budget"
        ),
        metadata={"scenario": str(scenario)},
    )


def evaluate_medium_term_watermark(
    *,
    item_count: int,
    token_estimate: int,
    policy: MemoryWatermarkPolicy | None = None,
) -> MemoryWatermarkDecision:
    active_policy = policy or MemoryWatermarkPolicy()
    over_items = item_count > active_policy.medium_max_items_per_employee
    over_tokens = token_estimate > active_policy.medium_max_tokens_per_employee
    action = (
        MemoryWatermarkAction.CLEANUP_AND_PROMOTE
        if over_items or over_tokens
        else MemoryWatermarkAction.KEEP
    )
    limit = max(
        active_policy.medium_max_items_per_employee,
        active_policy.medium_max_tokens_per_employee,
    )
    current = max(item_count, token_estimate)
    reasons: list[str] = []
    if over_items:
        reasons.append("medium_term_item_count_exceeds_limit")
    if over_tokens:
        reasons.append("medium_term_token_estimate_exceeds_limit")
    return MemoryWatermarkDecision(
        layer=MemoryLayer.MEDIUM_TERM,
        action=action,
        limit=limit,
        current=current,
        reason="+".join(reasons) if reasons else "medium_term_within_limits",
        metadata={
            "item_count": item_count,
            "token_estimate": token_estimate,
            "max_items": active_policy.medium_max_items_per_employee,
            "max_tokens": active_policy.medium_max_tokens_per_employee,
        },
    )


def long_term_archive_action() -> MemoryWatermarkDecision:
    return MemoryWatermarkDecision(
        layer=MemoryLayer.LONG_TERM,
        action=MemoryWatermarkAction.ARCHIVE_TO_NAS_AND_DISTILL,
        limit=0,
        current=0,
        reason="long_term_memory_uses_nas_archive_and_distillation_handoff",
        metadata={
            "archive": "nas",
            "handoff": "obsidian,harness,assistant_distillation",
        },
    )
