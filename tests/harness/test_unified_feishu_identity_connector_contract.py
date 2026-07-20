from pathlib import Path

from harness.evaluator.kb_import_contract import (
    load_contract,
    validate_contract,
    verification_commands,
)

CONTRACT = Path("harness/contracts/unified_feishu_identity_connector.json")
NAS_COMPOSE = Path("deploy/nas-unified/compose.yml")
START_ALL = Path("start-all.sh")


def test_unified_feishu_identity_connector_contract_is_valid() -> None:
    contract = load_contract(CONTRACT)

    assert validate_contract(contract) == []


def test_unified_feishu_identity_connector_contract_covers_entrypoints() -> None:
    contract = load_contract(CONTRACT)

    architecture = contract["target_architecture"]
    assert architecture["identity_key"] == "tenant_key + union_id"
    assert architecture["oauth_purposes"] == [
        "employee_login",
        "desktop_login",
        "cloud_docs",
    ]
    assert contract["preserved_contracts"] == [
        "organization_permission_model",
        "desktop_employee_onboarding",
        "feishu_assistant_workbench",
    ]
    assert [item["id"] for item in contract["acceptance_criteria"]] == [
        "ufic-001",
        "ufic-002",
        "ufic-003",
        "ufic-004",
        "ufic-005",
        "ufic-006",
        "ufic-007",
    ]
    assert all(command.strip() for command in verification_commands(contract))


def test_nas_h5_runtime_points_to_the_unified_connector() -> None:
    compose = NAS_COMPOSE.read_text(encoding="utf-8")

    assert (
        'FEISHU_UNIFIED_CONNECTOR_BASE_URL: "https://dianchi2026.vercel.app"' in compose
    )
    assert (
        'FEISHU_UNIFIED_CONNECTOR_PROXY_URL: "http://aihubmix-aws-tunnel:7898"'
        in compose
    )
    assert 'FEISHU_ATTACHMENT_OAUTH_APP: "agent"' in compose


def test_nas_h5_supports_lan_pilot_and_domestic_domain_cutover() -> None:
    compose = NAS_COMPOSE.read_text(encoding="utf-8")
    launcher = START_ALL.read_text(encoding="utf-8")

    assert "cloudflared" not in compose.lower()
    assert '"127.0.0.1:6285:6185"' in compose
    assert "${DC_ASSISTANT_H5_ORIGIN:-http://192.168.1.35:6185}" in compose
    assert "https://*) export DC_ASSISTANT_H5_ORIGIN" in launcher
    assert "http://192.168.1.35:6185) export DC_ASSISTANT_H5_ORIGIN" in launcher

    architecture = load_contract(CONTRACT)["target_architecture"]
    assert architecture["workbench_origin"].startswith("NAS LAN origin")
    assert architecture["nas_ingress"].startswith("NAS LAN forward during phase 1")
