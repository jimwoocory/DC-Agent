"""Legacy compatibility shim for the retired dc_router routing adapter.

The production path is now:

``main.py -> dispatch.py -> routing/apply_decision.py -> cli_handlers.py``.

This module intentionally contains no execution logic. It only re-exports
the new boundaries for older imports while preventing the retired Hermes /
Harness CLI execution path from staying alive inside dc_router.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

try:  # pragma: no cover - exercised by direct file-load tests via fallback
    from .cli_handlers import (
        CLI_PROVIDER_PREFIX,
        DISABLED_LEGACY_CLI_BACKEND,
        DISABLED_LEGACY_CLI_PROVIDER_ID,
        DISABLED_LEGACY_CLI_REASON,
        GROK_BUILD_FALLBACK_PROVIDER_ID,
        build_cli_prompt,
        handle_disabled_legacy_cli_card_action,
        is_cli_provider,
        parse_cli_provider,
        start_queue_recovery,
        stop_queue_recovery,
    )
    from .config import load_config
    from .dispatch import dispatch
    from .routing import (
        AstrBotRouterClassifier,
        apply_decision,
        apply_provider_pin,
        build_envelope,
        parse_classifier_json,
    )
except ImportError:  # pragma: no cover - direct importlib file loading
    from data.plugins.dc_router.cli_handlers import (
        CLI_PROVIDER_PREFIX,
        DISABLED_LEGACY_CLI_BACKEND,
        DISABLED_LEGACY_CLI_PROVIDER_ID,
        DISABLED_LEGACY_CLI_REASON,
        GROK_BUILD_FALLBACK_PROVIDER_ID,
        build_cli_prompt,
        handle_disabled_legacy_cli_card_action,
        is_cli_provider,
        parse_cli_provider,
        start_queue_recovery,
        stop_queue_recovery,
    )
    from data.plugins.dc_router.config import load_config
    from data.plugins.dc_router.dispatch import dispatch
    from data.plugins.dc_router.routing import (
        AstrBotRouterClassifier,
        apply_decision,
        apply_provider_pin,
        build_envelope,
        parse_classifier_json,
    )


event_to_envelope = build_envelope


def create_dc_router(context: Any):
    """Build a DCRouter using the standalone AstrBot classifier adapter."""
    from dc_router_core.entrypoint import DCRouter

    return DCRouter(classifier=AstrBotRouterClassifier(context))


async def route_via_dc_router(
    context: Any,
    event: Any,
    dry_run: bool = True,
) -> bool:
    """Compatibility wrapper around the current dispatch entrypoint.

    New code should call ``dispatch.dispatch(context, event, cfg)`` directly.
    This wrapper exists for old imports only and does not carry the retired
    routing_adapter execution path.
    """
    cfg = replace(load_config(), enabled=True, dry_run=dry_run)
    result = await dispatch(context, event, cfg)
    return bool(result.handled)


__all__ = [
    "AstrBotRouterClassifier",
    "CLI_PROVIDER_PREFIX",
    "DISABLED_LEGACY_CLI_BACKEND",
    "DISABLED_LEGACY_CLI_PROVIDER_ID",
    "DISABLED_LEGACY_CLI_REASON",
    "GROK_BUILD_FALLBACK_PROVIDER_ID",
    "apply_decision",
    "apply_provider_pin",
    "build_cli_prompt",
    "build_envelope",
    "create_dc_router",
    "event_to_envelope",
    "handle_disabled_legacy_cli_card_action",
    "is_cli_provider",
    "parse_classifier_json",
    "parse_cli_provider",
    "route_via_dc_router",
    "start_queue_recovery",
    "stop_queue_recovery",
]
