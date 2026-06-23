from pathlib import Path

from harness.plugin_boundaries import PluginBoundaryRegistry


def test_default_plugin_boundaries_point_to_existing_plugin_dirs() -> None:
    registry = PluginBoundaryRegistry.default()

    assert registry.missing_plugin_dirs(Path("data/plugins")) == ()


def test_high_risk_plugins_have_machine_readable_roles_and_engine_owners() -> None:
    registry = PluginBoundaryRegistry.default()

    hermes = registry.get("hermes_bridge")
    assert hermes.role == "core_adapter"
    assert hermes.boundary_status == "runtime_service"
    assert hermes.adapter_entrypoints
    assert "harness.hermes_bridge" in hermes.engine_modules

    router = registry.get("dc_router")
    assert router.role == "runtime_gateway"
    assert router.boundary_status == "mixed"
    assert "dc_router_core" in router.engine_modules
    assert "harness" in router.engine_modules
    # dc_router is the sole merged routing entrypoint and no longer depends on
    # the retired llm_router plugin.
    assert router.path == "data/plugins/dc_router"

    case = registry.get("case_plugin")
    assert case.role == "product_plugin"
    assert case.boundary_status == "thin_adapter"
    assert "dc_engines.case" in case.engine_modules

    feishu_channel = registry.get("feishu_channel_control")
    assert feishu_channel.role == "core_adapter"
    assert feishu_channel.boundary_status == "thin_adapter"
    assert "dc_engines.feishu_channel_control" in feishu_channel.engine_modules

    harness_runtime = registry.get("harness_runtime_plugin")
    assert harness_runtime.role == "runtime_gateway"
    assert harness_runtime.boundary_status == "thin_adapter"
    assert "dc_engines.harness" in harness_runtime.engine_modules

    harness_state = registry.get("harness_state_injector")
    assert harness_state.role == "core_adapter"
    assert harness_state.boundary_status == "thin_adapter"
    assert "dc_engines.harness.runtime_hooks" in harness_state.engine_modules

    harness_sensor = registry.get("harness_sensor_plugin")
    assert harness_sensor.role == "core_adapter"
    assert harness_sensor.boundary_status == "thin_adapter"
    assert "dc_engines.harness.runtime_hooks" in harness_sensor.engine_modules

    god_mode = registry.get("god_mode_plugin")
    assert god_mode.role == "ops_plugin"
    assert god_mode.boundary_status == "thin_adapter"
    assert "dc_engines.god_mode" in god_mode.engine_modules
    assert "feishu_card_action:god_mode_approval" in god_mode.adapter_entrypoints


def test_plugin_boundary_runtime_data_stays_outside_plugin_source_tree() -> None:
    registry = PluginBoundaryRegistry.default()

    for boundary in registry.boundaries():
        for pattern in boundary.runtime_data_patterns:
            assert not pattern.startswith(boundary.path)
            assert pattern.startswith("data/")


def test_plugin_boundary_contract_payload_is_stable_and_serializable() -> None:
    payload = PluginBoundaryRegistry.default().to_contract_payload()

    assert sorted(item["name"] for item in payload["plugins"]) == [
        "case_plugin",
        "content_sop_rule_review_plugin",
        "dc_hub",
        "dc_router",
        "department_workflow_plugin",
        "feishu_channel_control",
        "feishu_resource_plugin",
        "god_mode_plugin",
        "harness_runtime_plugin",
        "harness_sensor_plugin",
        "harness_state_injector",
        "hermes_bridge",
    ]
    assert all(item["responsibility"] for item in payload["plugins"])
    assert all(item["boundary_status"] for item in payload["plugins"])


def test_plugin_boundary_config_paths_are_declared_as_data_config() -> None:
    registry = PluginBoundaryRegistry.default()

    for boundary in registry.boundaries():
        for path in boundary.config_paths:
            assert path.startswith("data/config/")


def test_persona_factory_boundary_is_owned_by_dc_hub() -> None:
    registry = PluginBoundaryRegistry.default()

    boundary = registry.get("dc_hub")

    assert "filter.command:persona" in boundary.adapter_entrypoints
    assert "dc_engines.persona_factory" in boundary.engine_modules
    assert "data/persona_factory/" in boundary.runtime_data_patterns
