from pathlib import Path

from harness.evaluator.kb_import_contract import (
    load_contract,
    validate_contract,
    verification_commands,
)

CONTRACT = Path("harness/contracts/obsidian_memory_governance.json")


def _criteria_by_id() -> dict[str, dict]:
    contract = load_contract(CONTRACT)
    return {
        criterion["id"]: criterion for criterion in contract["acceptance_criteria"]
    }


def test_obsidian_memory_governance_contract_is_valid() -> None:
    contract = load_contract(CONTRACT)

    assert validate_contract(contract) == []


def test_obsidian_memory_governance_contract_has_unique_criteria_ids() -> None:
    contract = load_contract(CONTRACT)
    criterion_ids = [criterion["id"] for criterion in contract["acceptance_criteria"]]

    assert len(criterion_ids) == len(set(criterion_ids))


def test_obsidian_memory_governance_contract_points_to_required_verifiers() -> None:
    contract = load_contract(CONTRACT)

    assert verification_commands(contract) == [
        "uv run pytest dc_engines/tests/test_obsidian_memory_export_import.py::test_export_writes_review_note_with_required_frontmatter -q",
        "uv run pytest dc_engines/tests/test_obsidian_memory_export_import.py::test_import_is_idempotent_for_same_memory_id -q",
        "uv run pytest dc_engines/tests/test_obsidian_memory_promotion.py::test_promoter_only_promotes_approved_internal_memory -q",
        "uv run pytest dc_engines/tests/test_memory_governance_store.py::test_decision_and_audit_records_are_append_only -q",
        "uv run pytest dc_engines/tests/test_obsidian_memory_promotion.py::test_recall_filters_to_approved_by_default -q",
        "uv run pytest tests/harness/test_obsidian_memory_governance_contract.py::test_contract_preserves_existing_memory_assets -q",
        "uv run pytest tests/harness/test_obsidian_memory_governance_contract.py::test_contract_records_first_version_non_goals -q",
    ]


def test_contract_requires_observable_full_stack_boundaries() -> None:
    contract = load_contract(CONTRACT)
    architecture = contract["target_architecture"]

    assert architecture["raw_memory_index"] == "data/nas_memory.db"
    assert (
        architecture["human_governance_workspace"]
        == "ObsidianVault/40_MemoryGovernance"
    )
    assert architecture["governed_state_store"] == "data/governed_memory.db"
    assert "data/plugins/llm_router/dc_memory_context.py" in architecture[
        "production_recall"
    ]
    assert "append-only audit log" in architecture["promotion_outputs"]


def test_contract_preserves_existing_memory_assets() -> None:
    contract = load_contract(CONTRACT)

    expected_assets = {
        "data/nas_memory.db",
        "nas_sync/dc_memory_indexer.py",
        "ObsidianVault/00_RawRefs",
        "ObsidianVault/10_Index",
        "ObsidianVault/20_Bridges",
        "dc_engines/dc_engines/obsidian_review.py",
        "data/plugins/llm_router/dc_memory_context.py",
        "data/config/nas_memory_overrides.json",
        "harness/contracts/local_knowledge_base_phase2_rawrefs.json",
    }

    assert expected_assets <= set(contract["existing_assets_to_reuse"])


def test_contract_records_first_version_non_goals() -> None:
    contract = load_contract(CONTRACT)
    non_goals = "\n".join(contract["non_goals"])

    assert "native Obsidian plugin" in non_goals
    assert "realtime file watching" in non_goals
    assert "data/agy_review_candidates" in non_goals
    assert "automatic deletion or forgetting" in non_goals
    assert "bulk proactive review pings" in non_goals


def test_contract_requires_governance_note_frontmatter_and_audit() -> None:
    criteria = _criteria_by_id()

    export_description = criteria["omg-001"]["description"]
    audit_description = criteria["omg-004"]["description"]

    for required in (
        "memory_id",
        "source_path",
        "source_hash",
        "review_status",
        "sensitivity",
        "governance_version",
    ):
        assert required in export_description

    for required in ("actor", "action", "memory_id", "payload", "timestamp"):
        assert required in audit_description


def test_contract_requires_approved_only_runtime_recall() -> None:
    criteria = _criteria_by_id()

    description = criteria["omg-005"]["description"]

    assert "approved-only" in description
    assert "need_review" in description
    assert "sensitive_blocked" in description
    assert "secret" in description
    assert "explicit admin path" in description
