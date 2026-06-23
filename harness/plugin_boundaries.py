"""Machine-readable ownership boundaries for DC-Agent plugins.

The registry is intentionally small and explicit. It gives repository hygiene
checks and reviewers a stable way to tell adapter code, product entrypoints,
and runtime/local data apart without moving plugin code in a broad refactor.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

PluginRole = Literal["core_adapter", "product_plugin", "ops_plugin", "runtime_gateway"]
BoundaryStatus = Literal["thin_adapter", "mixed", "plugin_heavy", "runtime_service"]


@dataclass(frozen=True, slots=True)
class PluginBoundary:
    name: str
    role: PluginRole
    path: str
    boundary_status: BoundaryStatus
    responsibility: str
    adapter_entrypoints: tuple[str, ...] = ()
    engine_modules: tuple[str, ...] = ()
    config_paths: tuple[str, ...] = ()
    runtime_data_patterns: tuple[str, ...] = ()

    def to_contract_payload(self) -> dict[str, object]:
        return asdict(self)


class PluginBoundaryRegistry:
    def __init__(self, boundaries: Iterable[PluginBoundary]) -> None:
        self._boundaries = {boundary.name: boundary for boundary in boundaries}

    @classmethod
    def default(cls) -> PluginBoundaryRegistry:
        return cls(_DEFAULT_BOUNDARIES)

    def get(self, name: str) -> PluginBoundary:
        try:
            return self._boundaries[name]
        except KeyError as exc:
            raise KeyError(f"Unknown plugin boundary: {name}") from exc

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._boundaries))

    def boundaries(self) -> tuple[PluginBoundary, ...]:
        return tuple(self._boundaries[name] for name in self.names())

    def missing_plugin_dirs(self, plugin_root: str | Path) -> tuple[str, ...]:
        root = Path(plugin_root)
        return tuple(name for name in self.names() if not (root / name).is_dir())

    def to_contract_payload(self) -> dict[str, object]:
        return {
            "plugins": [
                boundary.to_contract_payload() for boundary in self.boundaries()
            ],
        }


_DEFAULT_BOUNDARIES: tuple[PluginBoundary, ...] = (
    PluginBoundary(
        name="hermes_bridge",
        role="core_adapter",
        path="data/plugins/hermes_bridge",
        boundary_status="runtime_service",
        responsibility=(
            "AstrBot-to-Hermes adapter for task dispatch, callback handling, "
            "and card finalization. Shared Harness runtime context is owned by "
            "harness_runtime_plugin."
        ),
        adapter_entrypoints=(
            "filter.event_message_type:ALL",
            "response_server:/hermes/callback",
        ),
        engine_modules=(
            "dc_engines.hermes_bridge_engine",
            "dc_engines.harness",
            "harness.hermes_bridge",
        ),
        config_paths=(
            "data/config/hermes_bridge_config.json",
            "data/config/hermes_bridge_config.example.json",
        ),
        runtime_data_patterns=(
            "data/hermes_sessions.db",
            "data/card_runtime/",
        ),
    ),
    PluginBoundary(
        name="harness_runtime_plugin",
        role="runtime_gateway",
        path="data/plugins/harness_runtime_plugin",
        boundary_status="thin_adapter",
        responsibility=(
            "Shared Harness runtime bootstrap for AstrBot plugins, including "
            "harness_engine, harness_store, harness memory promotion, and the "
            "legacy dispatch_task_to_hermes compatibility shim."
        ),
        adapter_entrypoints=("star.initialize:harness_runtime",),
        engine_modules=("dc_engines.harness",),
        runtime_data_patterns=(
            "data/harness.db",
            "data/harness_memory.db",
        ),
    ),
    PluginBoundary(
        name="harness_state_injector",
        role="core_adapter",
        path="data/plugins/harness_state_injector",
        boundary_status="thin_adapter",
        responsibility=(
            "AstrBot LLM-request hook adapter for Harness active-task and "
            "truth-guard prompt injection."
        ),
        adapter_entrypoints=("filter.on_llm_request:harness_state_injection",),
        engine_modules=("dc_engines.harness.runtime_hooks",),
    ),
    PluginBoundary(
        name="harness_sensor_plugin",
        role="core_adapter",
        path="data/plugins/harness_sensor_plugin",
        boundary_status="thin_adapter",
        responsibility=(
            "AstrBot LLM-response/decorating-result hook adapter for Harness "
            "task settlement and auto-completion gates."
        ),
        adapter_entrypoints=(
            "filter.on_llm_response:harness_sensor",
            "filter.on_decorating_result:harness_sensor",
        ),
        engine_modules=("dc_engines.harness.runtime_hooks",),
    ),
    PluginBoundary(
        name="dc_router",
        role="runtime_gateway",
        path="data/plugins/dc_router",
        boundary_status="mixed",
        responsibility=(
            "Unified intent classification and provider routing gateway. "
            "Merged from data/plugins/llm_router/; sole entry point for "
            "business/Ops platform messages. Owns circuit breakers for "
            "antigravity/qwen3.6-flash, QuotaGate queue recovery, and "
            "platform-level preprocessing (chitchat, card action, dept "
            "memory, tone, media, feishu channel)."
        ),
        adapter_entrypoints=(
            "filter.event_message_type:GROUP_MESSAGE|PRIVATE_MESSAGE",
            "feishu_card_action:dc_router",
        ),
        engine_modules=(
            "dc_router_core",
            "harness",
        ),
        config_paths=("data/config/dc_router_config.json",),
        runtime_data_patterns=(
            "data/antigravity_health.json",
            "data/antigravity_health_events.jsonl",
            "data/grok_worker_state.json",
            "data/dc_router_queue_recovery.lock",
        ),
    ),
    PluginBoundary(
        name="case_plugin",
        role="product_plugin",
        path="data/plugins/case_plugin",
        boundary_status="thin_adapter",
        responsibility="Case intake entrypoint and user-facing case archive commands.",
        adapter_entrypoints=("filter.command:case",),
        engine_modules=("dc_engines.case",),
        runtime_data_patterns=(
            "data/cases.db",
            "data/case_archives/",
        ),
    ),
    PluginBoundary(
        name="department_workflow_plugin",
        role="product_plugin",
        path="data/plugins/department_workflow_plugin",
        boundary_status="thin_adapter",
        responsibility="Department workflow commands and Content SOP user entrypoint.",
        adapter_entrypoints=("filter.event_message_type:ALL",),
        engine_modules=(
            "dc_engines.department_workflows",
            "dc_engines.harness",
        ),
        config_paths=("data/config/department_workflow_plugin_config.json",),
    ),
    PluginBoundary(
        name="content_sop_rule_review_plugin",
        role="ops_plugin",
        path="data/plugins/content_sop_rule_review_plugin",
        boundary_status="thin_adapter",
        responsibility="Human approval and rollback entrypoint for Content SOP runtime rule changes.",
        adapter_entrypoints=("feishu_card_action:content_sop_rule_review",),
        engine_modules=(
            "dc_engines.department_workflows.content_rule_proposals",
            "dc_engines.department_workflows.content_rule_overrides",
        ),
        config_paths=("data/config/content_sop_rule_review_plugin_config.json",),
        runtime_data_patterns=("data/content_sop_rule_proposals.db",),
    ),
    PluginBoundary(
        name="god_mode_plugin",
        role="ops_plugin",
        path="data/plugins/god_mode_plugin",
        boundary_status="thin_adapter",
        responsibility=(
            "Feishu /god administrator approval entrypoint for DC-Agent tool "
            "server actions. Planning, approval state, idempotency, and audit "
            "persistence are owned by dc_engines.god_mode."
        ),
        adapter_entrypoints=(
            "filter.command:god",
            "feishu_card_action:god_mode_approval",
        ),
        engine_modules=("dc_engines.god_mode",),
        config_paths=("data/config/god_mode_plugin_config.json",),
        runtime_data_patterns=("data/god_mode_audit.db",),
    ),
    PluginBoundary(
        name="dc_hub",
        role="ops_plugin",
        path="data/plugins/dc_hub",
        boundary_status="mixed",
        responsibility="Unified DC-Agent operations and dashboard aggregation surface.",
        adapter_entrypoints=(
            "dashboard:dc_hub",
            "filter.command:dc-hub",
            "filter.command:persona",
        ),
        engine_modules=(
            "dc_engines",
            "dc_engines.persona_factory",
            "harness",
        ),
        config_paths=("data/config/dc_hub_config.json",),
        runtime_data_patterns=(
            "data/dc_harness.db",
            "data/harness_tasks.db",
            "data/persona_factory/",
        ),
    ),
    PluginBoundary(
        name="feishu_channel_control",
        role="core_adapter",
        path="data/plugins/feishu_channel_control",
        boundary_status="thin_adapter",
        responsibility=(
            "Feishu/Lark ingress governance for pairing, group allowlists, "
            "trusted card-action gating, and router metadata annotation."
        ),
        adapter_entrypoints=(
            "filter.command:feishu-control",
            "filter.event_message_type:GROUP_MESSAGE|PRIVATE_MESSAGE",
        ),
        engine_modules=("dc_engines.feishu_channel_control",),
        config_paths=("data/config/feishu_channel_control_config.json",),
        runtime_data_patterns=("data/config/feishu_channel_control_state.json",),
    ),
    PluginBoundary(
        name="feishu_resource_plugin",
        role="product_plugin",
        path="data/plugins/feishu_resource_plugin",
        boundary_status="mixed",
        responsibility="Feishu resource retrieval entrypoint backed by dc_engines reader logic.",
        adapter_entrypoints=("filter.event_message_type:ALL",),
        engine_modules=("dc_engines.feishu_reader",),
    ),
)
