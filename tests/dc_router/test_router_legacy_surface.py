from __future__ import annotations

import json
from pathlib import Path

from dc_router_core.legacy_surface import (
    LegacySurfaceRisk,
    LegacySurfaceStatus,
    legacy_surface_contract,
    legacy_surfaces,
)
from dc_router_core.ops_provider_map import OPS_PROVIDER_MAP
from dc_router_core.ops_rules import OPS_KEYWORD_RULES, OPS_PREFIX_RULES
from dc_router_core.provider_map import DEFAULT_PROVIDER_MAP
from dc_router_core.rules import KEYWORD_RULES, PREFIX_RULES

CONTRACT_PATH = Path("harness/contracts/router_legacy_surface_v2.json")


def _contract() -> dict:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def test_legacy_surface_inventory_covers_contract_required_surfaces() -> None:
    contract = _contract()
    inventory = {surface.surface_id for surface in legacy_surfaces()}

    assert set(contract["required_surfaces"]) <= inventory
    assert len(inventory) == len(legacy_surfaces())


def test_high_risk_surfaces_require_replay_migration() -> None:
    contract_high_risk = set(_contract()["high_risk_surfaces"])
    inventory = {surface.surface_id: surface for surface in legacy_surfaces()}

    assert contract_high_risk == {
        surface.surface_id
        for surface in legacy_surfaces()
        if surface.risk is LegacySurfaceRisk.HIGH
    }
    for surface_id in contract_high_risk:
        surface = inventory[surface_id]
        assert surface.replay_required is True
        assert "replay" in surface.migration_action.lower()
        assert surface.status in {
            LegacySurfaceStatus.INVENTORIED,
            LegacySurfaceStatus.PARTIALLY_GATED,
            LegacySurfaceStatus.REPLAY_LOCKED,
        }


def test_legacy_surface_counts_match_router_tables() -> None:
    counts = {surface.surface_id: surface.count for surface in legacy_surfaces()}

    assert counts["business_prefix_rules"] == len(PREFIX_RULES)
    assert counts["business_keyword_rules"] == sum(
        len(keywords) for _, keywords, _ in KEYWORD_RULES
    )
    assert counts["business_provider_map"] == len(DEFAULT_PROVIDER_MAP)
    assert counts["ops_prefix_rules"] == len(OPS_PREFIX_RULES)
    assert counts["ops_keyword_rules"] == sum(
        len(keywords) for _, keywords, _ in OPS_KEYWORD_RULES
    )
    assert counts["ops_provider_map"] == len(OPS_PROVIDER_MAP)


def test_legacy_surface_contract_is_serializable() -> None:
    payload = legacy_surface_contract()

    assert payload["contract_id"] == "router_legacy_surface_v2"
    assert payload["risk_counts"]["high"] >= 1
    assert json.loads(json.dumps(payload, ensure_ascii=False)) == payload
