from pathlib import Path

from harness.evaluator.kb_import_contract import load_contract, validate_contract

CONTRACT = Path("harness/contracts/executor_settlement.json")


def test_executor_settlement_contract_is_valid() -> None:
    contract = load_contract(CONTRACT)

    assert validate_contract(contract) == []
    assert contract["runtime_model"]["interface"] == ["begin", "settle", "reconcile"]


def test_production_adapters_use_executor_settlement_interface() -> None:
    paths = (
        Path("data/plugins/dc_router/preprocessing/media_route.py"),
        Path("data/plugins/gpt_image_plugin/main.py"),
        Path("data/plugins/dreamina_plugin/main.py"),
        Path("data/plugins/dc_router/cli_handlers.py"),
        Path("data/plugins/hermes_bridge/hermes_bridge.py"),
    )

    for path in paths:
        source = path.read_text(encoding="utf-8")
        assert "executor_settlement" in source, path


def test_executor_adapters_do_not_own_scheduling() -> None:
    context = Path("CONTEXT.md").read_text(encoding="utf-8")
    contract = load_contract(CONTRACT)

    assert "**Executor Settlement**" in context
    assert any("scheduler" in item for item in contract["non_goals"])
