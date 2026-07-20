import json
from pathlib import Path

CONTRACT_PATH = (
    Path(__file__).resolve().parents[2]
    / "harness"
    / "contracts"
    / "feishu_desktop_messenger.json"
)


def test_feishu_desktop_messenger_contract_has_stable_acceptance_boundary() -> None:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))

    assert contract["contract_id"] == "feishu_desktop_messenger"
    criterion_ids = [criterion["id"] for criterion in contract["acceptance_criteria"]]
    assert criterion_ids == [
        "feishu-desktop-messenger-001",
        "feishu-desktop-messenger-002",
        "feishu-desktop-messenger-003",
        "feishu-desktop-messenger-004",
        "feishu-desktop-messenger-005",
        "feishu-desktop-messenger-006",
    ]
    assert (
        "copying or reverse engineering Feishu native desktop code"
        in contract["scope"]["excluded"]
    )
    assert any(
        "complete personal inbox" in item for item in contract["scope"]["excluded"]
    )
    assert any("global navigation" in item for item in contract["scope"]["included"])
