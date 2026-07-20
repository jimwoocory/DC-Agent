from pathlib import Path

from harness.evaluator.kb_import_contract import (
    load_contract,
    validate_contract,
    verification_commands,
)

CONTRACT = Path("harness/contracts/obsidian_three_level_taxonomy.json")


def _contract() -> dict:
    return load_contract(CONTRACT)


def test_obsidian_three_level_taxonomy_contract_is_valid() -> None:
    assert validate_contract(_contract()) == []


def test_obsidian_three_level_taxonomy_contract_has_unique_criteria_ids() -> None:
    criteria = _contract()["acceptance_criteria"]
    criterion_ids = [criterion["id"] for criterion in criteria]

    assert len(criterion_ids) == len(set(criterion_ids))


def test_obsidian_three_level_taxonomy_contract_verifies_the_full_loop() -> None:
    commands = verification_commands(_contract())

    assert commands == [
        "uv run pytest tests/test_generate_obsidian_refs.py::test_company_knowledge_taxonomy_is_valid_and_complete -q",
        "uv run pytest tests/test_generate_obsidian_refs.py::test_classify_document_returns_auditable_paths_and_safe_fallbacks -q",
        "uv run pytest tests/test_generate_obsidian_refs.py::test_write_three_level_taxonomy_builds_all_levels_and_leaf_links -q",
        "uv run pytest tests/test_generate_obsidian_refs.py::test_render_raw_ref_includes_three_level_taxonomy -q",
        "uv run pytest tests/test_generate_obsidian_refs.py::test_classify_document_applies_user_confirmed_business_ownership_rules -q",
    ]


def test_obsidian_three_level_taxonomy_contract_keeps_safe_boundaries() -> None:
    boundaries = _contract()["boundaries"]

    assert "without mutation" in boundaries["source_data"]
    assert "configured vault root" in boundaries["generated_output"]
    assert boundaries["secondary_dimensions"] == [
        "entity",
        "project",
        "person",
        "department",
        "document_type",
    ]
    assert "待人工确认" in boundaries["fallback_policy"]
