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
    / "feishu_business_mvp.json"
)


def test_feishu_business_mvp_contract_is_valid() -> None:
    contract = load_contract(CONTRACT_PATH)

    assert validate_contract(contract) == []


def test_feishu_business_mvp_contract_has_unique_criteria_ids() -> None:
    contract = load_contract(CONTRACT_PATH)
    criterion_ids = [criterion["id"] for criterion in contract["acceptance_criteria"]]

    assert len(criterion_ids) == len(set(criterion_ids))


def test_feishu_business_mvp_contract_lists_required_commands() -> None:
    contract = load_contract(CONTRACT_PATH)

    assert verification_commands(contract) == [
        "uv run pytest dc_engines/tests/test_feishu_business_mvp.py -q",
        "uv run pytest dc_engines/tests/test_feishu_business_feishu_adapters.py -q",
        "uv run pytest tests/test_feishu_business_plugins.py -q",
        "uv run pytest tests/harness/test_feishu_business_mvp_contract.py -q",
    ]


def test_feishu_business_mvp_contract_documents_runtime_wiring() -> None:
    contract = load_contract(CONTRACT_PATH)
    wiring = contract["runtime_wiring"]

    assert wiring["engine_package"] == "dc_engines/dc_engines/feishu_business_mvp"
    assert (
        wiring["workflow_runner"]
        == "dc_engines/dc_engines/feishu_business_mvp/workflows.py"
    )
    assert wiring["automation_script"] == "scripts-tools/feishu_business_mvp.py"
    assert (
        wiring["business_config_template"]
        == "data/config/feishu_business_mvp.example.json"
    )
    assert "data/plugins/admin_asset_plugin/main.py" in wiring["plugins"]
    assert "finance_approval_records" in wiring["database_tables"]
    assert "business_notification_logs" in wiring["database_tables"]
    assert "dc_engines.feishu_hub.call" in wiring["feishu_hub_entrypoints"]
    assert wiring["dashboard_entries"] == ["综合行政", "财务审批", "飞书集成健康"]
    assert (
        wiring["run_endpoint"]
        == "/api/plug/feishu_business_report_plugin/feishu_business/run"
    )
    assert "preflight" in wiring["workflow_actions"]
    assert "weekly-report" in wiring["workflow_actions"]
    assert "app_secret" in wiring["sensitive_config_rule"]
