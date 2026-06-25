"""Legacy router surface inventory for Router Decision Framework v2."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from dc_router_core.ops_provider_map import OPS_PROVIDER_MAP
from dc_router_core.ops_rules import OPS_KEYWORD_RULES, OPS_PREFIX_RULES
from dc_router_core.provider_map import DEFAULT_PROVIDER_MAP
from dc_router_core.rules import KEYWORD_RULES, PREFIX_RULES


class LegacySurfaceRisk(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class LegacySurfaceStatus(StrEnum):
    INVENTORIED = "inventoried"
    PARTIALLY_GATED = "partially_gated"
    REPLAY_LOCKED = "replay_locked"


@dataclass(frozen=True, slots=True)
class LegacySurface:
    surface_id: str
    category: str
    module: str
    decision_power: str
    risk: LegacySurfaceRisk
    status: LegacySurfaceStatus
    migration_action: str
    replay_required: bool
    count: int = 0

    def to_contract(self) -> dict[str, object]:
        return {
            "surface_id": self.surface_id,
            "category": self.category,
            "module": self.module,
            "decision_power": self.decision_power,
            "risk": self.risk.value,
            "status": self.status.value,
            "migration_action": self.migration_action,
            "replay_required": self.replay_required,
            "count": self.count,
        }


def legacy_surfaces() -> tuple[LegacySurface, ...]:
    """Return the v2 inventory of legacy router decision surfaces."""

    return (
        LegacySurface(
            surface_id="business_prefix_rules",
            category="business_intent",
            module="dc_router_core/rules.py",
            decision_power="direct_intent_override",
            risk=LegacySurfaceRisk.MEDIUM,
            status=LegacySurfaceStatus.INVENTORIED,
            migration_action="Keep as explicit user override; add replay for risky queue/provider outcomes before changing.",
            replay_required=True,
            count=len(PREFIX_RULES),
        ),
        LegacySurface(
            surface_id="business_document_link_rule",
            category="business_intent",
            module="dc_router_core/rules.py",
            decision_power="direct_multimodal_intent",
            risk=LegacySurfaceRisk.LOW,
            status=LegacySurfaceStatus.INVENTORIED,
            migration_action="Keep as strong object-reference signal; express as object_reference/source_object replay.",
            replay_required=True,
            count=1,
        ),
        LegacySurface(
            surface_id="business_keyword_rules",
            category="business_intent",
            module="dc_router_core/rules.py",
            decision_power="direct_intent_match",
            risk=LegacySurfaceRisk.HIGH,
            status=LegacySurfaceStatus.INVENTORIED,
            migration_action="Split by risk and migrate ambiguous matches into observation slots plus replay expectations.",
            replay_required=True,
            count=sum(len(keywords) for _, keywords, _ in KEYWORD_RULES),
        ),
        LegacySurface(
            surface_id="business_department_workflow_rules",
            category="department_workflow",
            module="dc_router_core/department_requirements.py",
            decision_power="direct_workflow_metadata_and_intent",
            risk=LegacySurfaceRisk.HIGH,
            status=LegacySurfaceStatus.PARTIALLY_GATED,
            migration_action="Continue converting department matches into context constraints, with workflow dispatch locked only by replay.",
            replay_required=True,
            count=8,
        ),
        LegacySurface(
            surface_id="business_content_sop_rule",
            category="business_intent",
            module="dc_router_core/content_sop.py",
            decision_power="direct_creative_intent",
            risk=LegacySurfaceRisk.MEDIUM,
            status=LegacySurfaceStatus.INVENTORIED,
            migration_action="Keep deterministic content typing, but add replay for media/department conflicts before broadening.",
            replay_required=True,
            count=1,
        ),
        LegacySurface(
            surface_id="business_classifier_fallback",
            category="classifier",
            module="dc_router_core/entrypoint.py",
            decision_power="fallback_intent_after_threshold",
            risk=LegacySurfaceRisk.MEDIUM,
            status=LegacySurfaceStatus.INVENTORIED,
            migration_action="Keep thresholded fallback only; never allow classifier output to bypass rule gate or replay promotion.",
            replay_required=True,
            count=1,
        ),
        LegacySurface(
            surface_id="business_provider_map",
            category="provider_resolution",
            module="dc_router_core/provider_map.py",
            decision_power="intent_to_provider_action_depth",
            risk=LegacySurfaceRisk.HIGH,
            status=LegacySurfaceStatus.PARTIALLY_GATED,
            migration_action="Keep retired legacy CLI ids out of default business intents; audit remaining provider changes through rule gate and replay.",
            replay_required=True,
            count=len(DEFAULT_PROVIDER_MAP),
        ),
        LegacySurface(
            surface_id="ops_prefix_rules",
            category="ops_intent",
            module="dc_router_core/ops_rules.py",
            decision_power="direct_ops_intent_override",
            risk=LegacySurfaceRisk.LOW,
            status=LegacySurfaceStatus.INVENTORIED,
            migration_action="Keep as explicit DevOps operator override; add replay before any provider/action changes.",
            replay_required=True,
            count=len(OPS_PREFIX_RULES),
        ),
        LegacySurface(
            surface_id="ops_keyword_rules",
            category="ops_intent",
            module="dc_router_core/ops_rules.py",
            decision_power="direct_ops_intent_match",
            risk=LegacySurfaceRisk.MEDIUM,
            status=LegacySurfaceStatus.INVENTORIED,
            migration_action="Add ops replay for queue/status/debug ambiguity before editing keywords.",
            replay_required=True,
            count=sum(len(keywords) for _, keywords, _ in OPS_KEYWORD_RULES),
        ),
        LegacySurface(
            surface_id="ops_provider_map",
            category="provider_resolution",
            module="dc_router_core/ops_provider_map.py",
            decision_power="ops_intent_to_provider_action_depth",
            risk=LegacySurfaceRisk.LOW,
            status=LegacySurfaceStatus.INVENTORIED,
            migration_action="Keep direct Codex routing; verify via ops replay before changing provider map.",
            replay_required=True,
            count=len(OPS_PROVIDER_MAP),
        ),
        LegacySurface(
            surface_id="route_arbiter",
            category="arbitration",
            module="harness/route_arbiter.py",
            decision_power="provider_depth_action_override",
            risk=LegacySurfaceRisk.HIGH,
            status=LegacySurfaceStatus.INVENTORIED,
            migration_action="Inventory quota/circuit mutations; require replay for queue/provider overrides and preserve rule gate metadata.",
            replay_required=True,
            count=1,
        ),
    )


def legacy_surface_contract() -> dict[str, object]:
    surfaces = legacy_surfaces()
    return {
        "contract_id": "router_legacy_surface_v2",
        "version": "2.0.0",
        "status": "active",
        "surfaces": [surface.to_contract() for surface in surfaces],
        "risk_counts": _risk_counts(surfaces),
    }


def _risk_counts(surfaces: tuple[LegacySurface, ...]) -> dict[str, int]:
    counts = {risk.value: 0 for risk in LegacySurfaceRisk}
    for surface in surfaces:
        counts[surface.risk.value] += 1
    return counts


__all__ = [
    "LegacySurface",
    "LegacySurfaceRisk",
    "LegacySurfaceStatus",
    "legacy_surface_contract",
    "legacy_surfaces",
]
