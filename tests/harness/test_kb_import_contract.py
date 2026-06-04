import json

from harness.evaluator.kb_import_contract import (
    DEFAULT_CONTRACT,
    load_contract,
    validate_contract,
    verification_commands,
)


def test_knowledge_base_import_contract_is_valid():
    contract = load_contract(DEFAULT_CONTRACT)

    assert validate_contract(contract) == []


def test_knowledge_base_import_contract_has_unique_criteria_ids():
    contract = load_contract(DEFAULT_CONTRACT)
    criterion_ids = [criterion["id"] for criterion in contract["acceptance_criteria"]]

    assert len(criterion_ids) == len(set(criterion_ids))


def test_knowledge_base_import_contract_points_to_pytest_verifiers():
    contract = load_contract(DEFAULT_CONTRACT)

    assert verification_commands(contract) == [
        "uv run pytest tests/test_kb_import.py::test_import_documents -q",
        "uv run pytest tests/test_kb_import.py::test_import_documents_returns_friendly_failure_message -q",
        "uv run pytest tests/unit/test_sparse_retriever.py tests/unit/test_faiss_vec_db.py -q",
        "uv run pytest tests/test_kb_import.py::test_import_documents_passes_source_path_to_kb_helper tests/test_kb_import.py::test_upload_document_persists_source_path_as_file_path -q",
        "uv run pytest tests/unit/test_kb_source_citations.py -q",
        "uv run pytest tests/unit/test_astr_main_agent.py::TestApplyKb::test_apply_kb_without_agentic_mode tests/unit/test_astr_main_agent.py::TestApplyKb::test_knowledge_base_tool_description_requires_source_citations -q",
        "uv run pytest tests/unit/test_kb_source_citations.py::test_fixture_import_retrieval_context_requires_source_citation -q",
        "uv run pytest tests/unit/test_kb_source_citations.py::test_formatted_context_includes_obsidian_wikilink_for_source_document -q",
    ]


def test_knowledge_base_import_contract_requires_source_metadata():
    contract = load_contract(DEFAULT_CONTRACT)
    criteria_by_id = {
        criterion["id"]: criterion for criterion in contract["acceptance_criteria"]
    }

    criterion = criteria_by_id["kb-import-004"]

    assert "source_path" in criterion["description"]
    assert (
        criterion["verification"]
        == "uv run pytest tests/test_kb_import.py::test_import_documents_passes_source_path_to_kb_helper tests/test_kb_import.py::test_upload_document_persists_source_path_as_file_path -q"
    )


def test_knowledge_base_import_contract_requires_source_citation_output():
    contract = load_contract(DEFAULT_CONTRACT)
    criteria_by_id = {
        criterion["id"]: criterion for criterion in contract["acceptance_criteria"]
    }

    criterion = criteria_by_id["kb-import-005"]

    assert "source_path" in criterion["description"]
    assert (
        criterion["verification"]
        == "uv run pytest tests/unit/test_kb_source_citations.py -q"
    )


def test_knowledge_base_import_contract_requires_agent_citation_instruction():
    contract = load_contract(DEFAULT_CONTRACT)
    criteria_by_id = {
        criterion["id"]: criterion for criterion in contract["acceptance_criteria"]
    }

    criterion = criteria_by_id["kb-import-006"]

    assert "cite" in criterion["description"].lower()
    assert "source_path" in criterion["description"]
    assert (
        criterion["verification"]
        == "uv run pytest tests/unit/test_astr_main_agent.py::TestApplyKb::test_apply_kb_without_agentic_mode tests/unit/test_astr_main_agent.py::TestApplyKb::test_knowledge_base_tool_description_requires_source_citations -q"
    )


def test_knowledge_base_import_contract_requires_fixture_e2e_citation_flow():
    contract = load_contract(DEFAULT_CONTRACT)
    criteria_by_id = {
        criterion["id"]: criterion for criterion in contract["acceptance_criteria"]
    }

    criterion = criteria_by_id["kb-import-007"]

    assert "fixture" in criterion["description"].lower()
    assert "source_path" in criterion["description"]
    assert (
        criterion["verification"]
        == "uv run pytest tests/unit/test_kb_source_citations.py::test_fixture_import_retrieval_context_requires_source_citation -q"
    )


def test_knowledge_base_import_contract_requires_obsidian_wikilink_citation():
    contract = load_contract(DEFAULT_CONTRACT)
    criteria_by_id = {
        criterion["id"]: criterion for criterion in contract["acceptance_criteria"]
    }

    criterion = criteria_by_id["kb-import-008"]

    assert "Obsidian" in criterion["description"]
    assert "[[" in criterion["description"]
    assert (
        criterion["verification"]
        == "uv run pytest tests/unit/test_kb_source_citations.py::test_formatted_context_includes_obsidian_wikilink_for_source_document -q"
    )


def test_contract_validator_rejects_missing_required_keys(tmp_path):
    contract_path = tmp_path / "broken.json"
    contract_path.write_text(
        json.dumps({"feature": "knowledge_base_import"}),
        encoding="utf-8",
    )
    contract = load_contract(contract_path)

    errors = validate_contract(contract)

    assert "missing top-level keys: acceptance_criteria, goal, verification" in errors
