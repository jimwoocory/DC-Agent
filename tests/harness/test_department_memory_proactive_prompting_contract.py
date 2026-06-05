import json
from pathlib import Path

from harness.evaluator.kb_import_contract import (
    load_contract,
    validate_contract,
    verification_commands,
)

CONTRACT = Path("harness/contracts/department_memory_proactive_prompting.json")


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
        "uv run pytest data/plugins/llm_router/test_dc_router_path.py::test_department_memory_keyword_prompts_before_injection data/plugins/llm_router/test_dc_router_path.py::test_department_memory_confirmation_injects_original_request data/plugins/llm_router/test_dc_router_path.py::test_department_memory_card_action_confirms_pending_prompt data/plugins/llm_router/test_dc_router_path.py::test_department_memory_card_action_dismisses_pending_prompt data/plugins/llm_router/test_dc_router_path.py::test_department_memory_card_action_rejects_untrusted_payload data/plugins/llm_router/test_dc_router_path.py::test_department_memory_prompt_sends_confirm_card_when_lark data/plugins/llm_router/test_dc_router_path.py::test_explicit_memory_lookup_skips_department_prompt -q",
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
