from astrbot.core.runtime_context.watermarks import (
    LarkChatScenario,
    MemoryLayer,
    MemoryWatermarkAction,
    MemoryWatermarkPolicy,
    evaluate_medium_term_watermark,
    evaluate_short_term_watermark,
    long_term_archive_action,
)


def test_short_term_lark_casual_context_within_budget_is_kept() -> None:
    decision = evaluate_short_term_watermark(
        current_tokens=8_000,
        scenario=LarkChatScenario.CASUAL,
    )

    assert decision.layer == MemoryLayer.SHORT_TERM
    assert decision.action == MemoryWatermarkAction.KEEP
    assert decision.limit == 12_000
    assert decision.exceeded is False


def test_short_term_lark_casual_context_over_budget_triggers_trim() -> None:
    decision = evaluate_short_term_watermark(
        current_tokens=100_000,
        scenario=LarkChatScenario.CASUAL,
    )

    assert decision.action == MemoryWatermarkAction.COMPRESS_OR_TRIM
    assert decision.reason == "short_term_context_exceeds_lark_budget"
    assert decision.exceeded is True


def test_short_term_lark_business_and_complex_have_higher_budgets() -> None:
    business = evaluate_short_term_watermark(
        current_tokens=18_000,
        scenario=LarkChatScenario.BUSINESS,
    )
    complex_task = evaluate_short_term_watermark(
        current_tokens=28_000,
        scenario=LarkChatScenario.COMPLEX,
    )

    assert business.limit == 24_000
    assert business.action == MemoryWatermarkAction.KEEP
    assert complex_task.limit == 32_000
    assert complex_task.action == MemoryWatermarkAction.KEEP


def test_medium_term_over_item_limit_triggers_cleanup_and_promotion() -> None:
    policy = MemoryWatermarkPolicy(
        medium_max_items_per_employee=2,
        medium_max_tokens_per_employee=10_000,
    )

    decision = evaluate_medium_term_watermark(
        item_count=3,
        token_estimate=500,
        policy=policy,
    )

    assert decision.layer == MemoryLayer.MEDIUM_TERM
    assert decision.action == MemoryWatermarkAction.CLEANUP_AND_PROMOTE
    assert "item_count" in decision.reason


def test_medium_term_over_token_limit_triggers_cleanup_and_promotion() -> None:
    policy = MemoryWatermarkPolicy(
        medium_max_items_per_employee=50,
        medium_max_tokens_per_employee=1_000,
    )

    decision = evaluate_medium_term_watermark(
        item_count=5,
        token_estimate=1_500,
        policy=policy,
    )

    assert decision.action == MemoryWatermarkAction.CLEANUP_AND_PROMOTE
    assert "token_estimate" in decision.reason


def test_long_term_action_is_archive_and_distillation_handoff() -> None:
    decision = long_term_archive_action()

    assert decision.layer == MemoryLayer.LONG_TERM
    assert decision.action == MemoryWatermarkAction.ARCHIVE_TO_NAS_AND_DISTILL
    assert decision.metadata["archive"] == "nas"
    assert "assistant_distillation" in str(decision.metadata["handoff"])
