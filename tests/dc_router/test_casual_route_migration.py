"""Regression tests for retiring legacy CLI from default business routes.

P2: 把 aihubmix/qwen3.6-flash 退出闲聊兜底主选。
P3: 2026-06-25 取消旧本地 CLI 默认业务入口。
落地动作：
  - CASUAL / WORK_PREFLIGHT / OPS_WRITING 改为 aihubmix/qwen3.7-max
  - 旧本地 CLI 只保留历史 provider id 与旧卡片取消 surface
  - qwen3.6-flash 不再出现在 CASUAL/OPS_WRITING 主选路径上

测试运行方式（走工程化 pytest）::

    cd /Users/dianchi/DC-Agent
    .venv/bin/python -m pytest tests/dc_router/test_casual_route_migration.py -v
"""

from __future__ import annotations

# ruff: noqa: E402
import sys
from pathlib import Path

import pytest

# 阶段 5H (2026-06-11): dc_router/ shim 包仍存在, 没问题; llm_router/
# 已并入 data/plugins/dc_router/ AstrBot plugin.
_ROOT = Path(__file__).resolve().parents[2]
# 必须把 project root 放到 sys.path[0], 否则 data/plugins/dc_router/ 会被
# python 当成 ``dc_router`` 包 (没有 classifier.py 等模块) 撞上. 这里用
# canonical ``dc_router_core`` import 避开 shim 冲突.
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from data.plugins.dc_router.cli_handlers import (
    DISABLED_LEGACY_CLI_BACKEND,
    DISABLED_LEGACY_CLI_PROVIDER_ID,
    parse_cli_provider,
)
from dc_router_core.provider_map import (
    AIHUBMIX_QWEN_FLASH,
    AIHUBMIX_QWEN_MAX,
    get_provider_route,
)
from dc_router_core.taxonomy import RouteAction, RouteDepth, RouterIntent


def test_casual_route_uses_qwen_max_as_primary() -> None:
    """CASUAL 主选必须是 Qwen Max，不能回到 旧 CLI 或 qwen3.6-flash。"""
    route = get_provider_route(RouterIntent.CASUAL)

    assert route.provider_id == AIHUBMIX_QWEN_MAX
    assert route.provider_id != AIHUBMIX_QWEN_FLASH, (
        "qwen3.6-flash 必须退出 CASUAL 兜底主选"
    )
    assert not route.provider_id.startswith("cli/antigravity/")
    assert route.depth is RouteDepth.DIRECT
    assert route.action is RouteAction.ANSWER


def test_ops_writing_route_uses_qwen_max_as_primary() -> None:
    """OPS_WRITING 默认不再走旧本地 CLI。"""
    route = get_provider_route(RouterIntent.OPS_WRITING)

    assert route.provider_id == AIHUBMIX_QWEN_MAX
    assert route.provider_id != AIHUBMIX_QWEN_FLASH, (
        "qwen3.6-flash 必须退出 OPS_WRITING 主选"
    )
    assert not route.provider_id.startswith("cli/antigravity/")


def test_work_preflight_uses_qwen_max() -> None:
    """WORK_PREFLIGHT 默认不再走旧本地 CLI。"""
    route = get_provider_route(RouterIntent.WORK_PREFLIGHT)
    assert route.provider_id == AIHUBMIX_QWEN_MAX
    assert not route.provider_id.startswith("cli/antigravity/")


def test_legacy_cli_provider_id_is_disabled() -> None:
    """Legacy CLI ids must parse as disabled, not executable."""
    backend, _, _ = parse_cli_provider(DISABLED_LEGACY_CLI_PROVIDER_ID)
    assert backend == DISABLED_LEGACY_CLI_BACKEND


def test_active_dispatch_path_does_not_import_legacy_routing_adapter() -> None:
    """The production router path must stay independent from routing_adapter.py."""
    plugin_dir = _ROOT / "data" / "plugins" / "dc_router"
    active_files = [
        plugin_dir / "main.py",
        plugin_dir / "plugin.py",
        plugin_dir / "dispatch.py",
        plugin_dir / "routing" / "__init__.py",
        plugin_dir / "routing" / "apply_decision.py",
    ]
    for path in active_files:
        assert "routing_adapter" not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "intent",
    [RouterIntent.CASUAL, RouterIntent.OPS_WRITING],
)
def test_qwen_not_used_for_casual_or_ops_writing(intent: RouterIntent) -> None:
    """qwen3.6-flash 不能出现在 CASUAL / OPS_WRITING 主选路径上。"""
    route = get_provider_route(intent)
    assert AIHUBMIX_QWEN_FLASH not in route.provider_id, (
        f"{intent.value} 仍在用 qwen3.6-flash ({route.provider_id})，"
        "P2 目标是把 qwen 退出兜底主选"
    )
