from pathlib import Path

from harness.evaluator.kb_import_contract import (
    load_contract,
    validate_contract,
    verification_commands,
)

CONTRACT = Path("harness/contracts/short_term_context_priority.json")
CORE_REGRESSION = (
    "uv run pytest tests/unit/test_astr_main_agent.py::"
    "TestBuildMainAgent::"
    "test_build_main_agent_demotes_dc_memory_below_recent_history -q"
)
HARNESS_REGRESSION = (
    "uv run pytest tests/harness/test_short_term_context_priority_contract.py -q"
)
LARK_REGRESSION = (
    "uv run pytest tests/unit/test_astr_main_agent.py::"
    "TestBuildMainAgent::"
    "test_build_main_agent_demotes_dc_memory_for_lark_short_feedback -q"
)
ROUTER_MEMORY_QUERY_REGRESSION = (
    "uv run pytest tests/unit/test_runtime_context_memory_query.py::"
    "test_build_memory_retrieval_query_combines_history_and_short_feedback -q"
)
STRUCTURED_EVENT_EXTRA_REGRESSION = (
    "uv run pytest tests/unit/test_astr_main_agent.py::"
    "TestBuildMainAgent::"
    "test_build_main_agent_consumes_structured_memory_event_extra -q"
)
ASSEMBLER_ORDERING_REGRESSION = (
    "uv run pytest tests/unit/test_runtime_context_assembler.py::"
    "test_assembler_orders_deduplicates_and_bounds_event_sections -q"
)


def _contract() -> dict:
    return load_contract(CONTRACT)


def test_short_term_context_priority_contract_is_valid() -> None:
    contract = _contract()

    assert validate_contract(contract) == []


def test_short_term_context_priority_contract_has_unique_criteria_ids() -> None:
    contract = _contract()
    criterion_ids = [criterion["id"] for criterion in contract["acceptance_criteria"]]

    assert len(criterion_ids) == len(set(criterion_ids))


def test_short_term_context_priority_contract_points_to_required_verifiers() -> None:
    contract = _contract()
    commands = verification_commands(contract)

    assert CORE_REGRESSION in commands
    assert HARNESS_REGRESSION in commands
    assert LARK_REGRESSION in commands
    assert ROUTER_MEMORY_QUERY_REGRESSION in commands
    assert STRUCTURED_EVENT_EXTRA_REGRESSION in commands
    assert ASSEMBLER_ORDERING_REGRESSION in commands


def test_contract_records_memory_priority_boundaries() -> None:
    contract = _contract()
    boundaries = contract["runtime_boundaries"]

    assert boundaries["authoritative_recent_context"] == [
        "conversation history",
        "current user prompt",
    ]
    assert boundaries["lower_priority_reference"] == [
        "data/plugins/dc_router/memory_injection.py injected dc_agent_memory_context",
        "runtime_context_sections event extra",
    ]
    assert boundaries["retrieval_query_context"] == [
        "data/plugins/dc_router/dispatch.py recent conversation history",
        "current user prompt",
    ]
    assert boundaries["core_enforcement"] == [
        "astrbot/core/runtime_context/assembler.py",
        "astrbot/core/astr_main_agent.py",
    ]


def test_contract_requires_runtime_context_pipeline_files() -> None:
    contract = _contract()
    pipeline = contract["runtime_context_pipeline"]

    assert "astrbot/core/runtime_context/models.py" in pipeline["core_files"]
    assert "astrbot/core/runtime_context/assembler.py" in pipeline["core_files"]
    assert "astrbot/core/runtime_context/memory_query.py" in pipeline["core_files"]
    assert "data/plugins/dc_router/dispatch.py" in pipeline["router_files"]
    assert "data/plugins/dc_router/memory_injection.py" in pipeline["router_files"]
    assert pipeline["priority_order"] == [
        "current_user_message",
        "recent_conversation_history",
        "current_turn_attachments",
        "long_term_memory_reference",
        "knowledge_base_reference",
        "background_system_context",
    ]
    assert pipeline["save_policy"] == {
        "current_user_message": "save",
        "recent_conversation_history": "already_saved",
        "current_turn_attachments": "no_save",
        "long_term_memory_reference": "no_save",
        "knowledge_base_reference": "no_save",
        "background_system_context": "no_save",
    }
    assert pipeline["event_extra_interface"] == "runtime_context_sections"
    assert pipeline["structured_section_owner"] == (
        "astrbot.core.runtime_context.assembler.RuntimeContextAssembler"
    )


def test_contract_blocks_platform_only_or_query_only_fixes() -> None:
    contract = _contract()
    non_goals = "\n".join(contract["non_goals"])

    assert "WebChat-only" in non_goals
    assert "string concatenation in dc_memory_context" in non_goals
    assert "long-term memory" in non_goals
    assert "active brand" in non_goals


def test_core_regression_entry_point_exists() -> None:
    test_source = Path("tests/unit/test_astr_main_agent.py").read_text(encoding="utf-8")

    assert "test_build_main_agent_demotes_dc_memory_below_recent_history" in test_source
    assert "test_build_main_agent_demotes_dc_memory_for_lark_short_feedback" in (
        test_source
    )
    assert "LarkMessageEvent" in test_source
    assert "<dc_agent_memory_context>" in test_source
    assert "不满意" in test_source
    assert "五菱2026中秋" in test_source
    assert "东风柳汽活动方案" in test_source


def test_router_regression_entry_point_exists() -> None:
    router_test_source = Path(
        "tests/unit/test_runtime_context_memory_query.py"
    ).read_text(encoding="utf-8")
    router_source = Path("data/plugins/dc_router/dispatch.py").read_text(
        encoding="utf-8"
    )
    memory_query_source = Path(
        "astrbot/core/runtime_context/memory_query.py"
    ).read_text(encoding="utf-8")

    assert "test_build_memory_retrieval_query_combines_history_and_short_feedback" in (
        router_test_source
    )
    assert "build_memory_retrieval_query(" in router_source
    assert "query_text=memory_query_text" in router_source
    assert "def build_memory_retrieval_query" in memory_query_source
    assert "最近对话" in memory_query_source


def test_runtime_context_pipeline_entry_points_exist() -> None:
    assembler = Path("astrbot/core/runtime_context/assembler.py").read_text(
        encoding="utf-8"
    )
    memory_query = Path("astrbot/core/runtime_context/memory_query.py").read_text(
        encoding="utf-8"
    )
    main_agent = Path("astrbot/core/astr_main_agent.py").read_text(encoding="utf-8")
    router = Path("data/plugins/dc_router/dispatch.py").read_text(encoding="utf-8")

    assert "class RuntimeContextAssembler" in assembler
    assert "def build_memory_retrieval_query" in memory_query
    assert "RuntimeContextAssembler().normalize(req, event=event)" in main_agent
    assert "build_memory_retrieval_query(" in router


def test_runtime_context_logic_has_single_core_owner() -> None:
    main_agent = Path("astrbot/core/astr_main_agent.py").read_text(encoding="utf-8")
    router = Path("data/plugins/dc_router/dispatch.py").read_text(encoding="utf-8")
    assembler = Path("astrbot/core/runtime_context/assembler.py").read_text(
        encoding="utf-8"
    )
    memory_query = Path("astrbot/core/runtime_context/memory_query.py").read_text(
        encoding="utf-8"
    )

    assert "_normalize_dc_memory_context_priority" not in main_agent
    assert "_build_memory_retrieval_query" not in router
    assert "DC_MEMORY_CONTEXT_OPEN_MARKER" in assembler
    assert "def build_memory_retrieval_query" in memory_query


def test_core_implementation_demotes_memory_context() -> None:
    assembler_source = Path("astrbot/core/runtime_context/assembler.py").read_text(
        encoding="utf-8"
    )
    main_agent_source = Path("astrbot/core/astr_main_agent.py").read_text(
        encoding="utf-8"
    )

    assert "DC_MEMORY_CONTEXT_OPEN_MARKER" in assembler_source
    assert "DC_MEMORY_CONTEXT_CLOSE_MARKER" in assembler_source
    assert "DC_MEMORY_CONTEXT_PRIORITY_PROMPT" in assembler_source
    assert "TextPart(" in assembler_source
    assert ".mark_as_temp()" in assembler_source
    assert "DC_MEMORY_CONTEXT_OPEN_MARKER not in req.prompt" in assembler_source
    assert "RuntimeContextAssembler().normalize(req, event=event)" in main_agent_source
