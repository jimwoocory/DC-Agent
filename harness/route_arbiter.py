"""L3 route arbitration backed by the harness quota gate and circuit state.

DCRouter L1 classifies intent, L2 selects a ProviderRoute, and this L3 arbiter
keeps the intent unchanged while adapting execution to live scarce-resource
state:

- Circuit open, such as Antigravity CLI failure, swaps to the fallback provider.
- Busy quota, such as in-flight or cooldown resources, upgrades light intents to
  FRONT queueing and heavy intents to HERMES deep-task execution.
- Healthy resources pass through unchanged, matching PassThroughArbiter.

The arbiter reads quota availability only. Real admit/queue side effects still
happen in the downstream adapter at execution time.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace

from dc_router_core.entrypoint import ArbitrationResult, MessageEnvelope
from dc_router_core.taxonomy import RouteAction, RouteDepth
from harness.quota_gate import QuotaGate

# Same shape as antigravity_health.antigravity_allowed.
CircuitChecker = Callable[[], tuple[bool, str, dict]]

DEFAULT_HEAVY_INTENTS = frozenset({"deep_creative", "deep_insight"})


@dataclass(slots=True)
class QuotaGateArbiter:
    """L3 arbiter with injected dependencies for tests and platform rollout."""

    quota_gate: QuotaGate | None = None
    circuit_checker: CircuitChecker | None = None
    circuit_provider_prefix: str = "cli/antigravity/"
    fallback_provider_id: str = "aihubmix/gemini-3.5-flash"
    fallback_target_model: str = "gemini-3.5-flash"
    heavy_intents: frozenset[str] = DEFAULT_HEAVY_INTENTS

    async def arbitrate(
        self,
        route,
        envelope: MessageEnvelope,
        metadata: dict[str, str],
    ) -> ArbitrationResult:
        depth = getattr(route, "depth", RouteDepth.DIRECT)
        action = getattr(route, "action", RouteAction.ANSWER)

        circuit_result = self._maybe_circuit_fallback(route, metadata)
        if circuit_result is not None:
            return circuit_result

        quota_result = await self._maybe_quota_upgrade(
            route, metadata, depth=depth, action=action
        )
        if quota_result is not None:
            return quota_result

        return ArbitrationResult(
            route=route,
            action=action,
            depth=depth,
            metadata=metadata,
            reason="pass-through",
            source="arbiter",
        )

    def _maybe_circuit_fallback(
        self,
        route,
        metadata: dict[str, str],
    ) -> ArbitrationResult | None:
        if self.circuit_checker is None:
            return None
        provider_id = getattr(route, "provider_id", "")
        if not provider_id.startswith(self.circuit_provider_prefix):
            return None
        allowed, reason, state = self.circuit_checker()
        if allowed:
            return None
        new_metadata = dict(metadata)
        new_metadata["arbiter_circuit_fallback"] = "true"
        new_metadata["arbiter_circuit_reason"] = reason
        remaining = state.get("remaining_seconds")
        if remaining is not None:
            new_metadata["arbiter_circuit_remaining_seconds"] = str(remaining)
        return ArbitrationResult(
            route=replace(
                route,
                provider_id=self.fallback_provider_id,
                target_model=self.fallback_target_model,
            ),
            action=getattr(route, "action", RouteAction.ANSWER),
            depth=getattr(route, "depth", RouteDepth.DIRECT),
            metadata=new_metadata,
            reason=f"circuit open ({reason}); fallback to {self.fallback_provider_id}",
            source="arbiter",
        )

    async def _maybe_quota_upgrade(
        self,
        route,
        metadata: dict[str, str],
        *,
        depth: RouteDepth,
        action: RouteAction,
    ) -> ArbitrationResult | None:
        if self.quota_gate is None:
            return None
        resource_keys = tuple(getattr(route, "resource_keys", ()) or ())
        if not resource_keys:
            return None
        if await self.quota_gate.resources_available_now(resource_keys):
            return None

        intent = getattr(route, "intent", None)
        intent_value = getattr(intent, "value", str(intent or ""))
        new_metadata = dict(metadata)
        new_metadata["arbiter_quota_busy"] = "true"
        if intent_value in self.heavy_intents:
            new_depth, new_action = RouteDepth.HERMES, RouteAction.ENQUEUE_DEEP_TASK
            reason = "scarce resource busy; heavy intent escalated to Hermes deep task"
        else:
            new_depth, new_action = RouteDepth.FRONT, RouteAction.QUEUE_FRONT
            reason = "scarce resource busy; queued at front depth"
        return ArbitrationResult(
            route=route,
            action=new_action,
            depth=new_depth,
            metadata=new_metadata,
            reason=reason,
            source="arbiter",
        )
