from __future__ import annotations

from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_harness_runtime_bootstrap_owns_harness_context(tmp_path):
    from data.plugins.harness_runtime_plugin.main import ensure_harness_runtime

    context = SimpleNamespace(get_config=lambda: {})

    await ensure_harness_runtime(context, {"data_dir": str(tmp_path)})

    assert context.harness_engine is not None
    assert context.harness_store is not None
    assert (tmp_path / "harness.db").exists()
    assert (tmp_path / "harness_memory.db").exists()
    assert callable(context.dispatch_task_to_hermes)


@pytest.mark.asyncio
async def test_harness_runtime_dispatch_compat_uses_late_registered_adapter(tmp_path):
    from data.plugins.harness_runtime_plugin.main import ensure_harness_runtime

    calls = []

    async def adapter(*args, **kwargs):
        calls.append((args, kwargs))
        return True

    context = SimpleNamespace(get_config=lambda: {})

    await ensure_harness_runtime(context, {"data_dir": str(tmp_path)})
    context.hermes_task_dispatcher = adapter

    ok = await context.dispatch_task_to_hermes(
        "task-1",
        "project_followup",
        "brief",
        "umo",
        {"k": "v"},
    )

    assert ok is True
    assert calls == [
        (
            ("task-1", "project_followup", "brief", "umo", {"k": "v"}),
            {},
        )
    ]
