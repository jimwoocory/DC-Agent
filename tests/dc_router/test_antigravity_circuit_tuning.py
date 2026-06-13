"""P3 调紧 antigravity CLI 跳闸阈值：cli/grok-build 连续 timeout 直接开 circuit。

落地的硬规则：
  - antigravity timeout 1 次就开 circuit（独立 cooldown 3 分钟，比 auth 短）
  - grok_build 已有"timeout 1 次就开"（grok_worker._failure_cooldown），保持不变
  - 保留 transient_threshold=2 给"非 timeout 的 transient 错误"使用

测试运行方式::

    cd /Users/dianchi/DC-Agent
    .venv/bin/python -m pytest tests/dc_router/test_antigravity_circuit_tuning.py -v
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
# 阶段 5H (2026-06-11): llm_router/ 已并入 dc_router/ AstrBot plugin
# (参见 harness/contracts/routing_merge_contract.json). antigravity_health /
# grok_worker / cli_runner 这些模块全都搬过去了.
_PLUGIN_DIR = _ROOT / "data" / "plugins" / "dc_router"
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))


def _load(name: str) -> object:
    spec = importlib.util.spec_from_file_location(name, _PLUGIN_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_with_deps(name: str, *deps: str) -> object:
    """加载带 sibling 依赖的 plugin 模块（先注入 plugin 目录到 sys.path）。"""
    if str(_PLUGIN_DIR) not in sys.path:
        sys.path.insert(0, str(_PLUGIN_DIR))
    for dep in deps:
        _load(dep)
    return _load(name)


@pytest.fixture
def isolated_antigravity_health(tmp_path, monkeypatch):
    module = _load("antigravity_health")
    original_state = module._STATE_PATH
    original_history = module._HISTORY_PATH
    module._STATE_PATH = tmp_path / "antigravity_health.json"
    module._HISTORY_PATH = tmp_path / "antigravity_health_events.jsonl"
    monkeypatch.delenv("DC_ANTIGRAVITY_TIMEOUT_COOLDOWN_SECONDS", raising=False)
    monkeypatch.delenv("DC_ANTIGRAVITY_TRANSIENT_FAILURE_THRESHOLD", raising=False)
    try:
        yield module
    finally:
        module._STATE_PATH = original_state
        module._HISTORY_PATH = original_history


def test_antigravity_timeout_opens_circuit_immediately(
    isolated_antigravity_health,
) -> None:
    """1 次 antigravity timeout 就应该开 circuit，员工不再等 90s+90s+5min。"""
    state = isolated_antigravity_health.mark_antigravity_failure(error_code="timeout")

    assert state["status"] == "open", (
        f"1 次 timeout 应该立刻 open 状态，实际 {state['status']}"
    )
    assert state["consecutive_failures"] == 1
    assert state["last_error_code"] == "timeout"
    assert state["disabled_until"] > 0


def test_antigravity_timeout_uses_dedicated_cooldown(
    isolated_antigravity_health, monkeypatch
) -> None:
    """timeout 的 cooldown 应该是独立可调（默认 3 分钟），不沿用 auth 的 30 分钟。"""
    monkeypatch.setenv("DC_ANTIGRAVITY_TIMEOUT_COOLDOWN_SECONDS", "120")
    state = isolated_antigravity_health.mark_antigravity_failure(error_code="timeout")

    remaining = state["remaining_seconds"]
    # 允许 ±2s 误差（测试执行 + 状态保存）
    assert 110 <= remaining <= 130, (
        f"timeout cooldown 应为 120s（默认 180s 可被 env 覆盖），实际 {remaining}s"
    )


def test_antigravity_transient_still_needs_threshold_to_open(
    isolated_antigravity_health,
) -> None:
    """非 timeout 的 transient 错误（如 empty_response）仍按 transient_threshold 累积。"""
    # 1 次 empty_response：counter=1，未达 threshold=2，应是 degraded
    state1 = isolated_antigravity_health.mark_antigravity_failure(
        error_code="empty_response"
    )
    assert state1["status"] == "degraded", (
        f"第 1 次 transient 失败应是 degraded，实际 {state1['status']}"
    )
    assert state1["consecutive_failures"] == 1

    # 第 2 次 empty_response：counter=2，达到 threshold，应是 open
    state2 = isolated_antigravity_health.mark_antigravity_failure(
        error_code="empty_response"
    )
    assert state2["status"] == "open", (
        f"第 2 次 transient 失败应 open，实际 {state2['status']}"
    )
    assert state2["consecutive_failures"] == 2


def test_antigravity_terminal_codes_still_use_auth_cooldown(
    isolated_antigravity_health,
) -> None:
    """auth_required / bad_cli_args / bin_not_allowed 不受 timeout 调整影响。"""
    state = isolated_antigravity_health.mark_antigravity_failure(
        error_code="auth_required"
    )
    assert state["status"] == "open"
    # 默认 auth_cooldown=30min=1800s
    assert 1700 <= state["remaining_seconds"] <= 1810, (
        f"auth_required 应进入 30min 冷却，实际 {state['remaining_seconds']}s"
    )


def test_antigravity_success_resets_consecutive_failures(
    isolated_antigravity_health,
) -> None:
    """成功一次清空计数器，避免历史 timeout 拖到未来。"""
    isolated_antigravity_health.mark_antigravity_failure(error_code="timeout")
    state = isolated_antigravity_health.mark_antigravity_success(elapsed_sec=0.5)
    assert state["status"] == "healthy"
    assert state["consecutive_failures"] == 0
    assert state["available"] is True


def test_grok_worker_timeout_trips_circuit_on_first_hit() -> None:
    """cli/grok-build 的超时保护（grok_worker._failure_cooldown）保持"1 次就开"。"""
    # grok_worker 依赖 cli_runner，先注入 plugin 目录再加载
    module = _load_with_deps("grok_worker", "cli_runner")
    worker = module.GrokBuildWorker()
    # 1 次 timeout → 应返回非零 cooldown
    cooldown = worker._failure_cooldown(error_code="timeout", failures=1)
    assert cooldown > 0, f"grok 1 次 timeout 应进入 cooldown，实际 cooldown={cooldown}s"

    # 非 timeout 非 terminal 类错误（2 次累积）
    cooldown_other_1 = worker._failure_cooldown(error_code="exit_code", failures=1)
    assert cooldown_other_1 == 0, "1 次非 timeout 非 terminal 应无 cooldown"
    cooldown_other_2 = worker._failure_cooldown(error_code="exit_code", failures=2)
    assert cooldown_other_2 > 0, "2 次累积后应开 cooldown"


def test_antigravity_timeout_breaker_logs_history(
    isolated_antigravity_health,
) -> None:
    """timeout 触发跳闸时也要写 history，便于 dashboard 观察。"""
    isolated_antigravity_health.mark_antigravity_failure(error_code="timeout")
    history_path = isolated_antigravity_health._HISTORY_PATH
    assert history_path.exists()
    lines = history_path.read_text(encoding="utf-8").strip().splitlines()
    assert lines, "timeout 失败事件应写入 history"
    last = __import__("json").loads(lines[-1])
    assert last["error_code"] == "timeout"
    assert last["event"] == "failure"
    assert last["status"] == "open"
