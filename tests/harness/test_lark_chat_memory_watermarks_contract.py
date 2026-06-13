import json
from pathlib import Path

CONTRACT = Path("harness/contracts/lark_chat_memory_watermarks.json")


def _load_contract() -> dict:
    return json.loads(CONTRACT.read_text(encoding="utf-8"))


def test_lark_chat_memory_watermarks_contract_is_valid() -> None:
    contract = _load_contract()

    assert contract["contract_id"] == "lark_chat_memory_watermarks"
    assert contract["memory_layers"]["short_term"]["overflow_action"] == (
        "compress_or_trim"
    )
    assert contract["memory_layers"]["medium_term"]["overflow_action"] == (
        "cleanup_merge_and_promote_candidates"
    )
    assert contract["memory_layers"]["long_term"]["overflow_action"] == (
        "archive_to_nas_and_distill"
    )


def test_lark_chat_memory_watermarks_have_unique_criteria_ids() -> None:
    criteria = _load_contract()["acceptance_criteria"]
    ids = [item["id"] for item in criteria]

    assert len(ids) == len(set(ids))


def test_lark_chat_memory_watermarks_points_to_required_verifiers() -> None:
    verifiers = {
        item["verification"] for item in _load_contract()["acceptance_criteria"]
    }

    assert "uv run pytest tests/unit/test_runtime_context_watermarks.py -q" in verifiers
    assert "uv run pytest dc_engines/tests/test_chat_archive.py -q" in verifiers


def test_lark_chat_memory_watermarks_keep_archive_out_of_prompt_context() -> None:
    contract = _load_contract()

    assert (
        "knowledge/Chat/Lark Chat"
        in contract["memory_layers"]["long_term"]["nas_path_template"]
    )
    assert any("full employee chat archive" in item for item in contract["non_goals"])


def test_lark_chat_memory_watermarks_are_wired_into_runtime() -> None:
    main_agent_source = Path("astrbot/core/astr_main_agent.py").read_text(
        encoding="utf-8"
    )
    internal_stage_source = Path(
        "astrbot/core/pipeline/process_stage/method/agent_sub_stages/internal.py"
    ).read_text(encoding="utf-8")

    assert "_get_lark_context_budget_override" in main_agent_source
    assert "context_max_tokens_override" in main_agent_source
    assert "append_lark_chat_record" in internal_stage_source
    assert "evaluate_medium_term_watermark" in internal_stage_source
    assert "long_term_archive_action" in internal_stage_source
