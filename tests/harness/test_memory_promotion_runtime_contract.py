from pathlib import Path

from harness.evaluator.kb_import_contract import (
    load_contract,
    validate_contract,
    verification_commands,
)

CONTRACT = Path("harness/contracts/memory_promotion_runtime.json")

PROMOTION_REGRESSION = (
    "uv run pytest tests/harness/test_memory_promotion_runtime.py::"
    "test_llm_response_completes_task_and_promotes_memory -q"
)
ERROR_GUARD_REGRESSION = (
    "uv run pytest tests/harness/test_memory_promotion_runtime.py::"
    "test_llm_error_response_fails_task_without_memory -q"
)
MATERIAL_GATE_REGRESSION = (
    "uv run pytest tests/harness/test_memory_promotion_runtime.py::"
    "test_insufficient_material_response_blocks_task_without_memory -q"
)


def _contract() -> dict:
    return load_contract(CONTRACT)


def test_memory_promotion_runtime_contract_is_valid() -> None:
    assert validate_contract(_contract()) == []


def test_contract_points_to_required_verifiers() -> None:
    commands = verification_commands(_contract())

    assert PROMOTION_REGRESSION in commands
    assert ERROR_GUARD_REGRESSION in commands
    assert MATERIAL_GATE_REGRESSION in commands


def test_contract_records_promotion_non_goals() -> None:
    non_goals = "\n".join(_contract()["non_goals"])

    assert "review_required_by_default" in non_goals
    assert "failed or blocked" in non_goals


def test_promotion_chain_entry_points_exist() -> None:
    engine_source = Path("dc_engines/dc_engines/harness/engine.py").read_text(
        encoding="utf-8"
    )
    promoter_source = Path(
        "dc_engines/dc_engines/harness/memory_promotion.py"
    ).read_text(encoding="utf-8")
    sensor_source = Path("data/plugins/harness_sensor_plugin/main.py").read_text(
        encoding="utf-8"
    )
    bridge_source = Path("data/plugins/hermes_bridge/hermes_bridge.py").read_text(
        encoding="utf-8"
    )

    assert "_maybe_promote_memory" in engine_source
    assert "promote_from_task" in promoter_source
    assert "maybe_complete_harness_task" in sensor_source
    # 引擎构造时必须装上 promoter，否则晋升链路静默休眠
    assert "memory_promoter=promoter" in bridge_source


def test_result_decorate_stage_has_no_plugin_name_special_case() -> None:
    """The core pipeline must attribute stop propagation by handler state."""
    stage_source = Path("astrbot/core/pipeline/result_decorate/stage.py").read_text(
        encoding="utf-8"
    )

    assert "harness_sensor_plugin" not in stage_source
    assert "stopped_before_handler" in stage_source
