from pathlib import Path

from harness.evaluator.kb_import_contract import (
    load_contract,
    validate_contract,
    verification_commands,
)

CONTRACT = Path("harness/contracts/obsidian_governance_activation.json")


def _contract() -> dict:
    return load_contract(CONTRACT)


def test_obsidian_governance_activation_contract_is_valid() -> None:
    assert validate_contract(_contract()) == []


def test_contract_points_to_engine_verifiers() -> None:
    commands = "\n".join(verification_commands(_contract()))

    assert "test_obsidian_task_outcome_export.py" in commands
    assert "test_obsidian_memory_staleness.py" in commands


def test_contract_records_governance_non_goals() -> None:
    non_goals = "\n".join(_contract()["non_goals"])

    assert "human review" in non_goals
    assert "auto-delete" in non_goals
    assert "disappeared" in non_goals


def test_knowledge_cycle_wires_governance_steps() -> None:
    cycle_source = Path("scripts-watchdog/knowledge_cycle.py").read_text(
        encoding="utf-8"
    )

    for step in (
        "obsidian_governance_export",
        "obsidian_governance_export_tasks",
        "obsidian_governance_stale_scan",
        "obsidian_governance_import",
        "obsidian_governance_promote",
    ):
        assert f'"{step}"' in cycle_source

    # 开关声明式单一来源，在 STEP_CONFIG 固化前加载
    assert "knowledge_cycle.env" in cycle_source
    assert cycle_source.index("_bootstrap_step_env()") < cycle_source.index(
        "STEP_CONFIG:"
    )


def test_switch_file_enables_full_loop() -> None:
    env_text = Path("data/config/knowledge_cycle.env").read_text(encoding="utf-8")

    assert "KNOWLEDGE_ENABLE_OBSIDIAN_GOVERNANCE_EXPORT=1" in env_text
    assert "KNOWLEDGE_ENABLE_OBSIDIAN_GOVERNANCE_EXPORT_TASKS=1" in env_text
    assert "KNOWLEDGE_ENABLE_OBSIDIAN_GOVERNANCE_IMPORT=1" in env_text
    assert "KNOWLEDGE_ENABLE_OBSIDIAN_GOVERNANCE_PROMOTE=1" in env_text
    assert "KNOWLEDGE_OBSIDIAN_GOVERNANCE_PROMOTE_DRY_RUN=0" in env_text
    assert "KNOWLEDGE_ENABLE_OBSIDIAN_GOVERNANCE_STALE_SCAN=1" in env_text


def test_cli_exposes_new_subcommands() -> None:
    cli_source = Path("scripts-tools/obsidian_memory_governance.py").read_text(
        encoding="utf-8"
    )

    assert '"export-tasks"' in cli_source
    assert '"stale-scan"' in cli_source
    assert "command_export_tasks" in cli_source
    assert "command_stale_scan" in cli_source
