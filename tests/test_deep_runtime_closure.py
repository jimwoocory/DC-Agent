from __future__ import annotations

from harness.runtime_registry import RuntimeRegistry


def test_runtime_registry_reports_supported_status_known_runtimes() -> None:
    registry = RuntimeRegistry.from_env({})

    for runtime in ("claude_cli", "codex_cli"):
        status = registry.get(runtime)
        assert status.runtime == runtime
        assert status.implemented is True
        assert status.enabled is True
        assert status.reason == "available"
        assert status.adapter_wired is True

    for runtime in ("gemini_cli", "hermes_agent"):
        status = registry.get(runtime)
        assert status.runtime == runtime
        assert status.implemented is True
        assert status.enabled is False
        assert status.reason == "adapter_not_wired"
        assert status.adapter_wired is False


def test_runtime_registry_reports_disabled_and_unknown_runtimes() -> None:
    registry = RuntimeRegistry.from_env({"DC_DISABLE_HERMES_AGENT": "1"})

    disabled = registry.get("hermes_agent")
    assert disabled.implemented is True
    assert disabled.enabled is False
    assert disabled.reason == "disabled_by_env"
    assert disabled.adapter_wired is False

    unknown = registry.get("missing_runtime")
    assert unknown.runtime == "missing_runtime"
    assert unknown.implemented is False
    assert unknown.enabled is False
    assert unknown.reason == "unknown_runtime"
    assert unknown.adapter_wired is False


def test_runtime_status_serializes_to_dashboard_payload() -> None:
    status = RuntimeRegistry.from_env({}).get("claude_cli")

    assert status.to_dashboard_payload() == {
        "runtime": "claude_cli",
        "implemented": True,
        "enabled": True,
        "reason": "available",
        "adapter_wired": True,
    }


def test_runtime_registry_dashboard_payload_contains_named_runtimes() -> None:
    payload = RuntimeRegistry.from_env({}).to_dashboard_payload()
    ids = {item["runtime"] for item in payload["runtimes"]}

    assert ids == {"claude_cli", "codex_cli", "gemini_cli", "hermes_agent"}
