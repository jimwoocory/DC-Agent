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
    / "feishu_resource_content_retrieval.json"
)


def test_feishu_resource_content_retrieval_contract_is_valid() -> None:
    contract = load_contract(CONTRACT_PATH)

    assert validate_contract(contract) == []


def test_feishu_resource_content_retrieval_contract_has_unique_criteria_ids() -> None:
    contract = load_contract(CONTRACT_PATH)
    criterion_ids = [criterion["id"] for criterion in contract["acceptance_criteria"]]

    assert len(criterion_ids) == len(set(criterion_ids))


def test_feishu_resource_content_retrieval_contract_lists_required_commands() -> None:
    contract = load_contract(CONTRACT_PATH)

    assert verification_commands(contract) == [
        "uv run pytest dc_engines/tests/test_feishu_reader_content_retrieval.py -q",
        "uv run pytest tests/harness/test_feishu_resource_content_retrieval_contract.py -q",
        "uv run pytest tests/test_feishu_resource_plugin.py -q",
        "uv run ruff check dc_engines/dc_engines/feishu_reader/contracts.py dc_engines/dc_engines/feishu_reader/query_engine.py dc_engines/dc_engines/feishu_reader/__init__.py dc_engines/tests/test_feishu_reader_content_retrieval.py data/plugins/feishu_resource_plugin/main.py tests/test_feishu_resource_plugin.py tests/harness/test_feishu_resource_content_retrieval_contract.py",
    ]


def test_feishu_resource_content_retrieval_contract_documents_runtime_wiring() -> None:
    contract = load_contract(CONTRACT_PATH)
    wiring = contract["runtime_wiring"]

    assert "dc_engines/dc_engines/feishu_reader/contracts.py" in wiring["reader_files"]
    assert (
        "dc_engines/dc_engines/feishu_reader/query_engine.py" in wiring["reader_files"]
    )
    assert wiring["plugin_file"] == "data/plugins/feishu_resource_plugin/main.py"
    assert "retrieval_mode" in wiring["provenance_metadata_keys"]
    assert "credential_status" in wiring["provenance_metadata_keys"]
    assert "source_type" in wiring["harness_hit_fields"]
    assert "matched_snippet" in wiring["harness_hit_fields"]


def test_feishu_resource_content_retrieval_contract_documents_task_semantics() -> None:
    contract = load_contract(CONTRACT_PATH)
    semantics = contract["runtime_wiring"]["harness_task_semantics"]

    assert semantics["hit_effect"] == "attach_provenance_only"
    assert "complete_task" in semantics["forbidden_hit_methods"]
    assert "set_status" in semantics["forbidden_hit_methods"]
    assert semantics["missing_block_statuses"] == ["pending", "in_progress", "blocked"]
    assert semantics["review_required_lifecycle_mutation"] is False
