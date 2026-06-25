from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_ROUTING_ADAPTER = _ROOT / "data" / "plugins" / "dc_router" / "routing_adapter.py"


def _load_routing_adapter():
    spec = importlib.util.spec_from_file_location(
        "dc_router_legacy_adapter_under_test",
        _ROUTING_ADAPTER,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_legacy_routing_adapter_is_only_a_compatibility_shim() -> None:
    source = _ROUTING_ADAPTER.read_text(encoding="utf-8")

    assert "_run_harness_cli_job" not in source
    assert "_enqueue_or_run_harness_cli" not in source
    assert "_watch_queued_harness_cli_job" not in source
    assert "asyncio.create_subprocess_exec" not in source


def test_legacy_routing_adapter_reexports_new_router_boundaries() -> None:
    module = _load_routing_adapter()

    assert module.DISABLED_LEGACY_CLI_BACKEND == "disabled_legacy_cli"
    assert module.is_cli_provider("cli/antigravity/gemini-3.5-flash") is True
    assert module.parse_cli_provider("aihubmix/gemini-3.5-flash") == (
        "",
        "aihubmix/gemini-3.5-flash",
        None,
    )
    assert callable(module.apply_decision)
    assert callable(module.route_via_dc_router)
