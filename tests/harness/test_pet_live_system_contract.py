import json
from pathlib import Path


def test_pet_live_system_contract_declares_closed_loop_sources() -> None:
    contract_path = Path("harness/contracts/pet_live_system.json")

    contract = json.loads(contract_path.read_text(encoding="utf-8"))

    assert contract["contract_id"] == "pet_live_system"
    assert set(contract["sources"]) >= {
        "feishu",
        "astrbot",
        "router",
        "harness",
        "hermes",
        "obsidian",
        "desktop",
    }
    assert contract["required_identity_keys"] == ["employee_id", "pet_id"]
    assert "feishu_open_id" in contract["optional_identity_keys"]
    assert "router_decision_made" in contract["required_events"]
    assert "memory_promoted" in contract["required_events"]
    assert "assistant_distillation_observed" in contract["required_events"]
    assert contract["privacy_rules"]["forbid_full_message_text"] is True
