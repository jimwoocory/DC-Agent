from pathlib import Path

from harness.evaluator.kb_import_contract import (
    load_contract,
    validate_contract,
    verification_commands,
)

CONTRACT = Path("harness/contracts/feishu_god_mode_tool_server.json")


def test_feishu_god_mode_tool_server_contract_is_valid() -> None:
    contract = load_contract(CONTRACT)

    assert validate_contract(contract) == []


def test_feishu_god_mode_tool_server_contract_points_to_verifiers() -> None:
    contract = load_contract(CONTRACT)

    commands = verification_commands(contract)

    assert (
        "uv run pytest tests/test_god_mode.py::test_parse_god_command_variants -q"
        in commands
    )
    assert any(
        "tests/test_god_mode_plugin.py::test_admin_side_effect_request_sends_approval_card"
        in command
        for command in commands
    )
    assert (
        "uv run pytest tests/harness/test_feishu_god_mode_tool_server_contract.py -q"
        in commands
    )


def test_feishu_god_mode_tool_server_contract_declares_tool_fragments() -> None:
    contract = load_contract(CONTRACT)

    assert contract["tool_fragments"] == [
        "dc_agent_route_message",
        "dc_agent_query_memory",
        "dc_agent_start_workflow",
        "dc_agent_send_feishu_card",
        "dc_agent_check_task_status",
        "dc_agent_run_contract_check",
    ]


def test_feishu_god_mode_tool_server_contract_declares_safety_model() -> None:
    contract = load_contract(CONTRACT)
    safety = contract["safety_model"]

    assert safety["approval_card_source"] == "god_mode_approval"
    assert safety["approval_card_value_fields"] == [
        "source",
        "run_id",
        "action_id",
        "decision",
    ]
    assert safety["idempotency_key"] == ["run_id", "action_id"]
    assert safety["trusted_card_action_only"] is True
    assert "shell" in safety["side_effect_capabilities"]
