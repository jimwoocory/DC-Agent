from pathlib import Path

from harness.evaluator.kb_import_contract import (
    load_contract,
    validate_contract,
    verification_commands,
)

CONTRACT = Path("harness/contracts/truth_intake_runtime.json")


def test_truth_intake_runtime_contract_is_valid():
    contract = load_contract(CONTRACT)

    assert validate_contract(contract) == []


def test_truth_intake_runtime_contract_has_unique_criteria_ids():
    contract = load_contract(CONTRACT)
    criterion_ids = [criterion["id"] for criterion in contract["acceptance_criteria"]]

    assert len(criterion_ids) == len(set(criterion_ids))


def test_truth_intake_runtime_contract_covers_source_archive_tool_access():
    contract = load_contract(CONTRACT)
    criteria_by_id = {
        criterion["id"]: criterion for criterion in contract["acceptance_criteria"]
    }

    assert (
        "active truth-intake archive"
        in criteria_by_id["truth-intake-001"]["description"]
    )
    assert "source_archive_dir" in criteria_by_id["truth-intake-003"]["description"]
    assert "archive_dir" in criteria_by_id["truth-intake-004"]["description"]
    assert "instead of completing" in criteria_by_id["truth-intake-006"]["description"]


def test_truth_intake_runtime_contract_points_to_runtime_verifiers():
    contract = load_contract(CONTRACT)

    assert verification_commands(contract) == [
        "uv run pytest tests/test_computer_fs_tools.py::test_restricted_member_can_read_active_truth_intake_archive tests/test_computer_fs_tools.py::test_restricted_member_grep_defaults_include_active_truth_intake_archive -q",
        "uv run pytest tests/test_computer_fs_tools.py::test_restricted_member_broad_data_grep_narrows_to_active_truth_intake tests/test_computer_fs_tools.py::test_restricted_member_still_cannot_read_unrelated_data_path -q",
        "uv run pytest tests/test_harness_state_injector.py -q",
        "uv run env PYTHONPATH=.:dc_engines pytest data/plugins/llm_router/test_dc_router_path.py -k truth_intake -q",
        "uv run env PYTHONPATH=.:dc_engines pytest dc_engines/tests/test_harness_lifecycle.py::test_merge_payload_updates_task_and_records_event dc_engines/tests/test_harness_lifecycle.py::test_merge_payload_rejects_terminal_task -q",
        "uv run pytest dc_engines/tests/harness_sensor_plugin_truth_intake_test.py::test_sensor_classifies_missing_materials_as_insufficient dc_engines/tests/harness_sensor_plugin_truth_intake_test.py::test_sensor_blocks_instead_of_completes_when_materials_are_insufficient -q",
    ]
