from pathlib import Path

from harness.evaluator.kb_import_contract import (
    load_contract,
    validate_contract,
    verification_commands,
)

CONTRACT = Path("harness/contracts/desktop_employee_onboarding.json")


def test_desktop_employee_onboarding_contract_is_valid() -> None:
    contract = load_contract(CONTRACT)

    assert validate_contract(contract) == []


def test_desktop_employee_onboarding_contract_covers_the_delivery_loop() -> None:
    contract = load_contract(CONTRACT)

    assert [item["id"] for item in contract["acceptance_criteria"]] == [
        "desktop-onboarding-001",
        "desktop-onboarding-002",
        "desktop-onboarding-003",
        "desktop-onboarding-004",
        "desktop-onboarding-005",
        "desktop-onboarding-006",
    ]
    assert contract["preserved_contracts"] == [
        "organization_permission_model",
        "feishu_desktop_messenger",
        "pet_live_system",
    ]
    assert all(command.strip() for command in verification_commands(contract))
