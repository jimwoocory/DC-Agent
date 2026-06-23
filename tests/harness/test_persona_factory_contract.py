from pathlib import Path

from harness.evaluator.kb_import_contract import (
    load_contract,
    validate_contract,
    verification_commands,
)

CONTRACT = Path("harness/contracts/persona_factory.json")


def test_persona_factory_contract_is_valid() -> None:
    contract = load_contract(CONTRACT)

    assert validate_contract(contract) == []


def test_persona_factory_contract_documents_split() -> None:
    contract = load_contract(CONTRACT)
    architecture = contract["architecture"]

    assert architecture["core_module"] == "dc_engines.persona_factory"
    assert architecture["astrbot_entrypoint"] == "data/plugins/dc_hub"
    assert architecture["runtime_data_root"] == "data/persona_factory"
    assert (
        "human_review_required_before_registration"
        in architecture["required_boundaries"]
    )
    assert any(
        "dc_engines/tests/test_persona_factory.py" in command
        for command in verification_commands(contract)
    )
