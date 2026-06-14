import json
from pathlib import Path

from harness.evaluator.kb_import_contract import (
    load_contract,
    validate_contract,
    verification_commands,
)

CONTRACT = Path("harness/contracts/department_memory_proactive_prompting.json")
RUNTIME_VERIFIER = (
    "uv run pytest "
    "tests/dc_router/test_dispatch_pipeline.py::"
    "TestStage7DepartmentMemory::test_dept_memory_prompt_stops_dispatch "
    "tests/dc_router/test_dispatch_pipeline.py::"
    "TestStage8MemoryInjection::test_memory_injection_runs_when_dept_decision_says_inject "
    "tests/dc_router/test_dispatch_pipeline.py::"
    "TestStage8MemoryInjection::test_memory_injection_does_not_mutate_message_str "
    "tests/dc_router/test_dispatch_pipeline.py::"
    "TestStage1CardAction::test_card_action_text_routes_to_card_stage -q"
)


def test_department_memory_proactive_prompting_contract_is_valid() -> None:
    contract = load_contract(CONTRACT)

    assert validate_contract(contract) == []


def test_department_memory_proactive_prompting_contract_has_unique_criteria_ids() -> (
    None
):
    contract = load_contract(CONTRACT)
    criterion_ids = [criterion["id"] for criterion in contract["acceptance_criteria"]]

    assert len(criterion_ids) == len(set(criterion_ids))


def test_department_memory_proactive_prompting_contract_points_to_runtime_verifiers() -> (
    None
):
    contract = load_contract(CONTRACT)

    assert list(dict.fromkeys(verification_commands(contract))) == [
        "uv run pytest tests/harness/test_department_memory_proactive_prompting_contract.py -q",
        RUNTIME_VERIFIER,
    ]


def test_department_memory_proactive_prompting_contract_requires_safety_boundaries() -> (
    None
):
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    text = json.dumps(contract, ensure_ascii=False)

    for required in [
        "approved-only",
        "need_review",
        "sensitive_blocked",
        "explicit confirmation",
        "must not auto-inject",
        "append-only audit",
        "suggested",
        "confirmed",
        "dismissed",
        "expired",
        "blocked",
        "obsidian_memory_governance",
        "short_term_context_priority",
        "planning_content_sop",
        "client_content_sop",
    ]:
        assert required in text
