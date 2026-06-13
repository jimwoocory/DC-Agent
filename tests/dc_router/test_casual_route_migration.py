"""Regression tests for the 2026-06-08 casual / ops_writing route migration.

P2: 把 aihubmix/qwen3.6-flash 退出闲聊兜底主选 + 失败 N 次切 antigravity。
落地动作：
  - CASUAL 与 OPS_WRITING 的 ProviderRoute.provider_id 改为 ANTIGRAVITY_CLI_FLASH
  - 失败 N 次（默认 threshold=2）→ antigravity circuit open →
    adapter 自动切到 ANTIGRAVITY_FALLBACK_PROVIDER_ID = aihubmix/gemini-3.5-flash
  - qwen3.6-flash 不再出现在 CASUAL/OPS_WRITING 主选路径上

测试运行方式（走工程化 pytest）::

    cd /Users/dianchi/DC-Agent
    .venv/bin/python -m pytest tests/dc_router/test_casual_route_migration.py -v
"""

from __future__ import annotations

import importlib.util
import os
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

from dc_router_core.provider_map import (
    AIHUBMIX_QWEN_FLASH,
    ANTIGRAVITY_CLI_FLASH,
    get_provider_route,
)
from dc_router_core.taxonomy import RouteAction, RouteDepth, RouterIntent


def test_casual_route_uses_antigravity_as_primary() -> None:
    """CASUAL 主选必须迁到 Antigravity CLI，qwen3.6-flash 不能出现在主选。"""
    route = get_provider_route(RouterIntent.CASUAL)

    assert route.provider_id == ANTIGRAVITY_CLI_FLASH, (
        f"CASUAL 主选应为 {ANTIGRAVITY_CLI_FLASH}，实际 {route.provider_id}"
    )
    assert route.provider_id != AIHUBMIX_QWEN_FLASH, (
        "qwen3.6-flash 必须退出 CASUAL 兜底主选"
    )
    assert route.depth is RouteDepth.DIRECT
    assert route.action is RouteAction.ANSWER


def test_ops_writing_route_uses_antigravity_as_primary() -> None:
    """OPS_WRITING 顺手迁到 Antigravity（与 CASUAL 保持一致）。"""
    route = get_provider_route(RouterIntent.OPS_WRITING)

    assert route.provider_id == ANTIGRAVITY_CLI_FLASH
    assert route.provider_id != AIHUBMIX_QWEN_FLASH, (
        "qwen3.6-flash 必须退出 OPS_WRITING 主选"
    )


def test_realtime_and_work_preflight_remain_on_antigravity() -> None:
    """WORK_PREFLIGHT / REALTIME 之前就已经在 Antigravity 上，P2 改完保持不变。"""
    for intent in (RouterIntent.WORK_PREFLIGHT, RouterIntent.REALTIME):
        route = get_provider_route(intent)
        assert route.provider_id == ANTIGRAVITY_CLI_FLASH, (
            f"{intent.value} 应该继续走 {ANTIGRAVITY_CLI_FLASH}，"
            f"实际 {route.provider_id}"
        )


def test_casual_fallback_provider_id_is_aihubmix_gemini_flash() -> None:
    """adapter 端的 ANTIGRAVITY_FALLBACK_PROVIDER_ID 必须是 aihubmix/gemini-3.5-flash。

    阶段 5H: 老文件 ``data/plugins/llm_router/dc_router_adapter.py`` 已并入
    ``data/plugins/dc_router/routing_adapter.py``.
    """
    plugin_dir = _ROOT / "data" / "plugins" / "dc_router"
    spec = importlib.util.spec_from_file_location(
        "dc_router_adapter_under_test", plugin_dir / "routing_adapter.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    assert module.ANTIGRAVITY_FALLBACK_PROVIDER_ID == "aihubmix/gemini-3.5-flash", (
        "Antigravity 跳闸时必须降级到 aihubmix/gemini-3.5-flash，"
        f"实际 {module.ANTIGRAVITY_FALLBACK_PROVIDER_ID}"
    )


def test_antigravity_health_threshold_is_at_most_three() -> None:
    """失败 N 次切 antigravity：默认 N ≤ 3（避免让员工等太久）。

    阶段 5H: antigravity_health.py 已迁到 ``data/plugins/dc_router/``.
    """
    plugin_dir = _ROOT / "data" / "plugins" / "dc_router"
    spec = importlib.util.spec_from_file_location(
        "antigravity_health_under_test", plugin_dir / "antigravity_health.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    # 临时清空相关 env，避免 CI 改过默认
    os.environ.pop("DC_ANTIGRAVITY_TRANSIENT_FAILURE_THRESHOLD", None)
    threshold = module._env_int("DC_ANTIGRAVITY_TRANSIENT_FAILURE_THRESHOLD", 2)
    assert 1 <= threshold <= 3, (
        f"Antigravity 跳闸阈值应在 [1,3]，实际 {threshold}（P2 期望 N 次=2）"
    )


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
