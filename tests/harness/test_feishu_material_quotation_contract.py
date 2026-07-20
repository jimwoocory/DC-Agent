from pathlib import Path

from harness.evaluator.kb_import_contract import load_contract, validate_contract

CONTRACT = Path("harness/contracts/feishu_material_quotation.json")


def test_feishu_material_quotation_contract_is_valid() -> None:
    contract = load_contract(CONTRACT)

    assert validate_contract(contract) == []
    assert contract["scope"]["owner"] == "筹备组"
    assert "NAS projects/供应商价格库" in contract["scope"]["source_of_truth"]
    assert contract["scope"]["internal_surface"] == "筹备组内部测算台"
    assert contract["scope"]["result_surface"] == "市场部估价结果"
    assert contract["scope"]["output_status"] == "估价方案"
    assert contract["calculation_policy"]["missing_price"].startswith("待询价")
    assert "必须为 0" in contract["calculation_policy"]["market_result_gate"]
    assert contract["supplier_price_intake"]["review_status"] == "待复核"
    assert (
        contract["supplier_price_intake"]["archive"]["authoritative"]
        == "NAS projects/供应商价格库"
    )
    assert contract["supplier_price_intake"]["modes"] == [
        "手工单项",
        "手工多项",
        "上传原始报价资料",
    ]
    assert "保持空白" in contract["supplier_price_intake"]["unit_policy"]
    assert (
        contract["quotation_archive"]["root"] == "NAS knowledge/projects/筹备组物料报价"
    )
    assert "images" in contract["quotation_archive"]["draft_layout"]
    assert "10 MB" in contract["quotation_archive"]["image_policy"]
    assert "up to 6 images" in contract["quotation_archive"]["image_policy"]
    assert "Single or batch" in contract["quotation_archive"]["image_policy"]
    quotation_criteria = {
        criterion["id"]: criterion["description"]
        for criterion in contract["acceptance_criteria"]
    }
    assert "working local thumbnail previews" in quotation_criteria["fmq-013"]
    assert "click-to-enlarge lightbox" in quotation_criteria["fmq-013"]
    network_access = contract["network_access"]
    assert network_access["shared_origin"] == (
        "http://192.168.1.35:6185 during LAN phase; HTTPS origin configured "
        "by DC_ASSISTANT_H5_ORIGIN after domain cutover"
    )
    assert network_access["external_port"] == 6185
    assert network_access["forward_target"] == "http://127.0.0.1:6285"
    assert network_access["origin_environment"] == "DC_ASSISTANT_H5_ORIGIN"
    assert network_access["ingress"].startswith("NAS LAN forward during phase 1")
    assert network_access["container_binding"] == ("127.0.0.1:6285 -> dc-agent:6185")
    assert network_access["future_origin"] == "https://workbench.gx-dianchi.cn"
    assert network_access["public_nas_port_required"] is False
    assert network_access["connection_audit"]["fields"] == [
        "observed_at",
        "source_ip",
        "method",
        "status_code",
        "service",
    ]
    assert "capability_token" in network_access["connection_audit"]["forbidden"]


def test_feishu_material_quotation_contract_has_unique_criteria_ids() -> None:
    contract = load_contract(CONTRACT)
    criterion_ids = [criterion["id"] for criterion in contract["acceptance_criteria"]]

    assert len(criterion_ids) == len(set(criterion_ids))


def test_feishu_material_quotation_contract_separates_internal_and_market_data() -> (
    None
):
    boundaries = load_contract(CONTRACT)["surface_boundaries"]

    assert "supplier" in boundaries["preparation_internal"]
    assert "profit" in boundaries["preparation_internal"]
    assert "material images" in boundaries["preparation_internal"]
    assert boundaries["market_result"][:5] == [
        "project name",
        "client or brand",
        "final estimate",
        "validity",
        "estimate status",
    ]
    assert "material details" in boundaries["must_not_expose_on_market_result"]
    assert "supplier" in boundaries["must_not_expose_on_market_result"]
    assert "profit" in boundaries["must_not_expose_on_market_result"]
