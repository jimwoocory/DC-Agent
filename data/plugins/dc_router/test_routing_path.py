"""Compat entry: tests/dc_router/test_routing_path.py 取代了已删除的
data/plugins/llm_router/test_dc_router_path.py (2026-06-11 P3 重构).

这个文件被 scripts/agent-check.sh --profile targeted 在 line 112 显式调,
目的是保留一个稳定的 import smoke test 入口, 让 pytest 始终能 collect
到 dc_router 路由核心模块, 防止 llm_router → dc_router 合并期间出现
``ModuleNotFoundError: dc_router.routing.reasoning_prefix`` 之类的 import
错误而没人发现。

不是单元测试 — 是 import-only smoke test。真正的 reasoning prefix / dispatch
测试都在 tests/dc_router/test_reasoning_prefix.py / test_dispatch_pipeline.py
里。
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

# 容错 import 列表 — 任何一个失败都会让这个 smoke test 失败, 提醒 PR author
# 别把 import 链搞坏。
_REQUIRED_DC_ROUTER_MODULES: tuple[str, ...] = (
    "dc_router_core.entrypoint",
    "dc_router_core.provider_map",
    "dc_router_core.ops_provider_map",
    "dc_router_core.rules",
)


def test_dc_router_core_modules_importable() -> None:
    """The merged dc_router_core package must always import cleanly.

    这是路由合并 (P3 重构 2026-06-11) 的最后一道保险 — 如果有人不小心
    在 PR 里 import 了已删除的 ``llm_router`` 包, 这条会立刻挂掉.
    """
    for module_name in _REQUIRED_DC_ROUTER_MODULES:
        try:
            importlib.import_module(module_name)
        except Exception as exc:  # noqa: BLE001
            pytest.fail(
                f"import {module_name!r} failed — this is a regression in "
                f"the dc_router_core surface. Check the recent PR for "
                f"orphaned ``llm_router`` imports. Original error: {exc}"
            )


def test_no_llm_router_package_anywhere() -> None:
    """Regression guard: ``llm_router`` package must NOT exist anywhere.

    2026-06-11 P3 重构后, llm_router 已全部合并进 dc_router / dc_router_core.
    如果有新人重新引入 ``data/plugins/llm_router/``, 这条会失败.
    """
    repo_root = Path(__file__).resolve().parents[2]
    legacy_paths = [
        repo_root / "data" / "plugins" / "llm_router",
        repo_root / "data" / "plugins" / "dc_router" / "llm_router_compat.py",
    ]
    for legacy in legacy_paths:
        assert not legacy.exists(), (
            f"legacy llm_router path reappeared: {legacy}. "
            f"dc_router 是唯一入口; 请把 import 改成 dc_router / dc_router_core."
        )


def test_reasoning_prefix_table_keeps_xhigh_on_claude() -> None:
    """Cross-check 与 tests/dc_router/test_reasoning_prefix.py 同源 —
    这里用硬约束再保护一次, 防止 reasoning_prefix 模块完全没被 import
    (意味着 plugin 加载失败) 时这一支静默 pass.

    2026-06-11 用户反馈 ``#超深`` 走 codex 是不对的, 这条不变量是
    业务契约: ``#超深`` / ``#超高`` / ``#xhigh`` / ``#深度`` 必须走 Claude 系列,
    永不解析到 codex.
    """
    # 延迟 import 避免污染全局 test 顺序
    if "data.plugins.dc_router.routing.reasoning_prefix" in sys.modules:
        rp_mod = sys.modules["data.plugins.dc_router.routing.reasoning_prefix"]
    else:
        # 用 importlib 直接加载 (跟 tests/dc_router/test_reasoning_prefix.py 同款)
        import importlib.util

        # data/plugins/dc_router/test_routing_path.py
        # parents[0]=dc_router, parents[1]=plugins, parents[2]=data, parents[3]=repo root
        repo_root = Path(__file__).resolve().parents[3]
        plugin_root = repo_root / "data" / "plugins"
        if str(plugin_root) not in sys.path:
            sys.path.insert(0, str(plugin_root))
        spec = importlib.util.spec_from_file_location(
            "_rp_compat_entry",
            plugin_root / "dc_router" / "routing" / "reasoning_prefix.py",
        )
        assert spec is not None and spec.loader is not None, (
            "reasoning_prefix.py not found at "
            f"{plugin_root / 'dc_router' / 'routing' / 'reasoning_prefix.py'} — "
            "this means the routing module is gone, which is a critical regression"
        )
        rp_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rp_mod)

    # 不变量: xhigh 家族必须全部 pin 到 Claude
    xhigh_prefixes = ("#超深", "#超高", "#xhigh", "#深度")
    for prefix in xhigh_prefixes:
        provider = rp_mod._REASONING_PREFIX_PROVIDERS.get(prefix)
        assert provider is not None, f"prefix {prefix!r} missing from the table"
        assert "codex" not in provider, (
            f"#超深 family must never route to codex; got {provider!r} for {prefix!r}"
        )
        assert provider.startswith("aihubmix/claude"), (
            f"#超深 family must route to aihubmix/claude*; got {provider!r} for {prefix!r}"
        )


if __name__ == "__main__":
    # 允许 `python data/plugins/dc_router/test_routing_path.py` 直接跑
    raise SystemExit(pytest.main([__file__, "-v"]))
