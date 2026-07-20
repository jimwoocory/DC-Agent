from pathlib import Path

from harness.evaluator.kb_import_contract import load_contract, validate_contract

CONTRACT = Path("harness/contracts/assistant_historical_quotation.json")


def test_assistant_historical_quotation_contract_is_valid() -> None:
    contract = load_contract(CONTRACT)

    assert validate_contract(contract) == []


def test_assistant_historical_quotation_contract_has_unique_criteria_ids() -> None:
    contract = load_contract(CONTRACT)
    criterion_ids = [criterion["id"] for criterion in contract["acceptance_criteria"]]

    assert len(criterion_ids) == len(set(criterion_ids))


def test_assistant_historical_quotation_contract_records_quote_boundaries() -> None:
    policy = load_contract(CONTRACT)["runtime_policy"]

    assert policy["data_source"] == "data/nas_memory.db"
    assert (
        policy["source_of_truth"] == "NAS originals and indexed curated supplier notes"
    )
    assert policy["output_status"] == "报价草案"
    assert policy["missing_price_policy"] == "标记待询价，不编造单价"
    assert "税费、运输、安装、加急、损耗和利润" in policy["calculation_policy"]
    assert "来源" in policy["citation_policy"]
