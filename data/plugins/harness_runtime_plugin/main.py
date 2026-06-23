"""Harness runtime bootstrap for DC-Agent AstrBot plugins.

This plugin owns the shared Harness engine/store context wiring. Execution
adapters such as Hermes may register themselves later without owning Harness.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.star import Context, Star, register


def _plugin_data_dir(config: dict[str, Any] | None, context: Any) -> Path:
    cfg = config or {}
    data_dir_override = str(cfg.get("data_dir") or "").strip()
    if not data_dir_override:
        try:
            root_cfg = context.get_config() or {}
            hermes_cfg = root_cfg.get("hermes_bridge", {})
            if isinstance(hermes_cfg, dict):
                data_dir_override = str(hermes_cfg.get("data_dir") or "").strip()
        except Exception:  # noqa: BLE001
            data_dir_override = ""
    if data_dir_override:
        return Path(data_dir_override)
    return Path(__file__).resolve().parents[3] / "data"


async def _dispatch_task_to_registered_hermes(context: Any, *args: Any, **kwargs: Any):
    dispatcher = getattr(context, "hermes_task_dispatcher", None)
    if dispatcher is None:
        logger.warning("[harness_runtime] Hermes task dispatcher unavailable")
        return False
    return await dispatcher(*args, **kwargs)


async def ensure_harness_runtime(
    context: Any,
    config: dict[str, Any] | None = None,
) -> None:
    """Install shared Harness runtime objects on the AstrBot context.

    The legacy ``context.dispatch_task_to_hermes`` name is preserved as a
    compatibility shim, but it resolves the Hermes adapter lazily so Harness
    does not depend on Hermes plugin load order.
    """

    from dc_engines.harness import (
        HarnessEngine,
        HarnessMemoryPromoter,
        HarnessMemoryStore,
        HarnessTaskStore,
    )

    data_dir = _plugin_data_dir(config, context)
    data_dir.mkdir(parents=True, exist_ok=True)

    engine = getattr(context, "harness_engine", None)
    store = getattr(context, "harness_store", None)
    if engine is None or store is None:
        task_store = HarnessTaskStore(str(data_dir / "harness.db"))
        await task_store.initialize()
        memory_store = HarnessMemoryStore(str(data_dir / "harness_memory.db"))
        await memory_store.initialize()
        promoter = HarnessMemoryPromoter(memory_store)
        engine = HarnessEngine(task_store, memory_promoter=promoter)

        context.harness_engine = engine
        context.harness_store = task_store
        context.harness_memory_store = memory_store
        logger.info(
            "[harness_runtime] initialized Harness sidecar: %s + %s",
            data_dir / "harness.db",
            data_dir / "harness_memory.db",
        )

    if getattr(context, "dispatch_task_to_hermes", None) is None:

        async def _compat_dispatch(*args: Any, **kwargs: Any):
            return await _dispatch_task_to_registered_hermes(context, *args, **kwargs)

        context.dispatch_task_to_hermes = _compat_dispatch


@register(
    "harness_runtime_plugin",
    "dc_agent",
    "Harness runtime bootstrap and compatibility wiring",
    "1.0.0",
)
class HarnessRuntimePlugin(Star):
    def __init__(self, context: Context, config=None) -> None:
        super().__init__(context, config)
        self._config = config or {}

    async def initialize(self) -> None:
        await ensure_harness_runtime(self.context, self._config)
