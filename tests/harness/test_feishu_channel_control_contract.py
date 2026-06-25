from __future__ import annotations

from pathlib import Path

from harness.evaluator.kb_import_contract import (
    load_contract,
    validate_contract,
    verification_commands,
)

CONTRACT_PATH = (
    Path(__file__).resolve().parents[2]
    / "harness"
    / "contracts"
    / "feishu_channel_control.json"
)


def test_feishu_channel_control_contract_is_valid() -> None:
    contract = load_contract(CONTRACT_PATH)

    assert validate_contract(contract) == []


def test_feishu_channel_control_contract_has_unique_criteria_ids() -> None:
    contract = load_contract(CONTRACT_PATH)
    criterion_ids = [criterion["id"] for criterion in contract["acceptance_criteria"]]

    assert len(criterion_ids) == len(set(criterion_ids))


def test_feishu_channel_control_contract_lists_required_commands() -> None:
    contract = load_contract(CONTRACT_PATH)

    assert verification_commands(contract) == [
        "uv run pytest tests/test_feishu_channel_control.py -q",
        "uv run pytest tests/test_feishu_channel_control.py -q",
        "uv run pytest tests/test_feishu_channel_control.py -q",
        "uv run pytest tests/test_feishu_channel_control.py -q",
        "uv run pytest tests/test_feishu_channel_control.py -q",
    ]


def test_feishu_channel_control_contract_documents_runtime_wiring() -> None:
    contract = load_contract(CONTRACT_PATH)
    wiring = contract["runtime_wiring"]

    assert wiring["plugin_file"] == "data/plugins/feishu_channel_control/main.py"
    assert wiring["engine_file"] == "dc_engines/dc_engines/feishu_channel_control.py"
    assert wiring["hub_file"] == "data/plugins/dc_hub/main.py"
    assert "feishu_channel_agent_id" in wiring["event_metadata_keys"]
    assert "dc_chat_entry_allowed" in wiring["event_metadata_keys"]
    assert "trusted card action" in wiring["policies"]
