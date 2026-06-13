from pathlib import Path

from harness.evaluator.kb_import_contract import (
    load_contract,
    validate_contract,
    verification_commands,
)

CONTRACT = Path("harness/contracts/agent_skill_compatibility.json")


def test_agent_skill_compatibility_contract_is_valid() -> None:
    contract = load_contract(CONTRACT)

    assert validate_contract(contract) == []


def test_agent_skill_compatibility_contract_has_unique_criteria_ids() -> None:
    contract = load_contract(CONTRACT)
    criterion_ids = [criterion["id"] for criterion in contract["acceptance_criteria"]]

    assert len(criterion_ids) == len(set(criterion_ids))


def test_agent_skill_compatibility_contract_points_to_focused_verifiers() -> None:
    contract = load_contract(CONTRACT)

    expected = {
        "uv run pytest tests/test_skill_loader.py::test_list_skills_skips_dirs_without_skill_md tests/test_skill_loader.py::test_read_skill_card_basic tests/test_skill_loader.py::test_build_block_includes_header_and_skill -q",
        "uv run pytest tests/test_skill_loader.py::test_read_skill_card_preloads_agent_skill_references -q",
        "uv run pytest tests/test_skill_loader.py::test_build_block_truncates_real_long_skill tests/test_skill_loader.py::test_build_block_enforces_byte_budget_for_chinese_skill tests/test_skill_loader.py::test_read_skill_card_respects_max_chars_across_cache_hits tests/test_skill_loader.py::test_read_skill_card_preloads_agent_skill_references -q",
        "uv run pytest tests/test_skill_loader.py::test_read_skill_card_rejects_references_outside_skill_dir tests/test_skill_loader.py::test_read_skill_card_rejects_parent_traversal_inside_skill_dir -q",
        "uv run pytest tests/harness/test_agent_skill_compatibility_contract.py -q",
        "uv run pytest tests/test_skill_loader.py::test_list_skills_finds_bundled_obsidian_skills tests/test_skill_loader.py::test_real_obsidian_authoring_skills_include_upstream_license tests/test_skill_loader.py::test_execution_oriented_obsidian_skills_do_not_bypass_allowlist -q",
        "uv run pytest tests/test_skill_loader.py::test_match_obsidian_markdown_bypasses_business_intent_allowlist tests/test_skill_loader.py::test_match_obsidian_bases_bypasses_business_intent_allowlist tests/test_skill_loader.py::test_match_json_canvas_bypasses_business_intent_allowlist tests/test_skill_loader.py::test_global_obsidian_skills_ignore_generic_business_text tests/test_skill_loader.py::test_global_obsidian_skills_ignore_execution_style_obsidian_text tests/test_skill_preloader.py::test_inject_obsidian_skill_under_business_intent -q",
        "uv run pytest tests/test_sync_bundled_skills.py -q",
    }

    assert expected <= set(verification_commands(contract))


def test_agent_skill_compatibility_contract_documents_agent_skills_shape() -> None:
    contract = load_contract(CONTRACT)
    criteria_by_id = {
        criterion["id"]: criterion for criterion in contract["acceptance_criteria"]
    }
    rules = contract["compatibility_rules"]

    assert "SKILL.md" in criteria_by_id["agent-skill-compat-001"]["description"]
    assert "SKILL.md" in rules["package_root"]
    assert rules["bundled_source_root"] == "bundled/skills"
    assert rules["runtime_install_root"] == "data/skills"
    assert rules["runtime_installer"] == "scripts/sync_bundled_skills.py"
    assert "references/*.md" in rules["optional_references"]
    assert rules["installed_obsidian_authoring_skills"] == [
        "obsidian-markdown",
        "obsidian-bases",
        "json-canvas",
    ]
    assert rules["deferred_execution_skills"] == ["obsidian-cli", "defuddle"]


def test_agent_skill_compatibility_contract_requires_bounded_references() -> None:
    contract = load_contract(CONTRACT)
    criteria_by_id = {
        criterion["id"]: criterion for criterion in contract["acceptance_criteria"]
    }
    criterion = criteria_by_id["agent-skill-compat-003"]

    assert "bounded" in criterion["description"].lower()
    assert "byte budgets" in criterion["description"]
    assert "bounded subset" in contract["compatibility_rules"]["bounded_references"]


def test_agent_skill_compatibility_contract_requires_path_boundary() -> None:
    contract = load_contract(CONTRACT)
    criteria_by_id = {
        criterion["id"]: criterion for criterion in contract["acceptance_criteria"]
    }
    criterion = criteria_by_id["agent-skill-compat-004"]
    path_boundary = contract["compatibility_rules"]["path_boundary"]

    assert "outside the current skill directory" in criterion["description"]
    assert "inside the current skill directory" in path_boundary
    assert "parent traversal" in path_boundary
