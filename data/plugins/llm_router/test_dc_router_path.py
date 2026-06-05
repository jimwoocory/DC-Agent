# ruff: noqa: I001
"""dc-router 路径单元测试（步骤 5）。

跑法（用 AstrBot venv）::

    cd /Users/dianchi/DC-Agent
    .venv/bin/python data/plugins/llm_router/test_dc_router_path.py

覆盖:
- 开关配置读取（默认值、文件不存在、JSON 损坏的容错）
- DCRouter 路由决策（典型 case）
- QuotaGate 端到端（admit / complete / fail / 冷却）
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

# 让 plugin 自身 + DC-Agent 顶层都能 import
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)
_DC_AGENT_ROOT = "/Users/dianchi/DC-Agent"
if _DC_AGENT_ROOT not in sys.path:
    sys.path.insert(0, _DC_AGENT_ROOT)
_DC_ENGINES_ROOT = "/Users/dianchi/DC-Agent/dc_engines"
if _DC_ENGINES_ROOT not in sys.path:
    sys.path.insert(0, _DC_ENGINES_ROOT)


def _load_llm_router_plugin_main():
    plugin_main_path = Path(_PLUGIN_DIR) / "main.py"
    spec = importlib.util.spec_from_file_location(
        "llm_router_plugin_main_under_test",
        plugin_main_path,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def isolated_antigravity_health(tmp_path):
    import antigravity_health

    original_state_path = antigravity_health._STATE_PATH
    original_history_path = antigravity_health._HISTORY_PATH
    antigravity_health._STATE_PATH = tmp_path / "antigravity_health.json"
    antigravity_health._HISTORY_PATH = tmp_path / "antigravity_health_events.jsonl"
    try:
        yield antigravity_health
    finally:
        antigravity_health._STATE_PATH = original_state_path
        antigravity_health._HISTORY_PATH = original_history_path


# ─────────────────── 1. 配置读取测试 ───────────────────


def test_config_current_state_active() -> None:
    """当前配置只校验字段类型，具体开关由部署环境决定。"""
    cfg_path = Path(_DC_AGENT_ROOT) / "data" / "config" / "dc_router_config.json"
    assert cfg_path.exists(), "配置文件应该存在"
    with cfg_path.open() as f:
        data = json.load(f)
    assert isinstance(data["enabled"], bool), "enabled 必须是 bool"
    assert isinstance(data["dry_run"], bool), "dry_run 必须是 bool"
    assert data["fallback_on_error"] is True, "默认 fallback_on_error=true"
    print("  ✅ 当前配置 enabled/dry_run 字段存在且是 bool")


def test_dc_router_platform_allowlist() -> None:
    """统一 dc-router 入口允许业务小助手 + DevOps，推广机器人不接管。"""
    plugin_main = _load_llm_router_plugin_main()

    assert plugin_main._should_enter_dc_router("巅池-Agent小助手") is True
    assert plugin_main._should_enter_dc_router("巅池-技术（DevOps）") is True
    assert plugin_main._should_enter_dc_router("巅池-技术") is True
    assert plugin_main._should_enter_dc_router("巅池-推广 01") is False
    print("  ✅ dc-router 入口白名单放行业务小助手 + DevOps")


def test_config_missing_file_safe_default() -> None:
    """配置文件不存在时，helper 返回安全默认。"""
    # 直接调 _read_dc_router_config 但指向一个不存在的路径
    plugin_main = _load_llm_router_plugin_main()

    original_path = plugin_main._DC_ROUTER_CONFIG_PATH
    try:
        plugin_main._DC_ROUTER_CONFIG_PATH = "/tmp/nonexistent_dc_router_config.json"
        cfg = plugin_main._read_dc_router_config()
        assert cfg["enabled"] is False
        assert cfg["fallback_on_error"] is True
        print("  ✅ 文件不存在时安全默认 enabled=false")
    finally:
        plugin_main._DC_ROUTER_CONFIG_PATH = original_path


def test_config_broken_json_safe_default() -> None:
    """JSON 损坏时，helper 返回安全默认。"""
    plugin_main = _load_llm_router_plugin_main()

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        f.write("{ this is not json")
        broken_path = f.name

    original_path = plugin_main._DC_ROUTER_CONFIG_PATH
    try:
        plugin_main._DC_ROUTER_CONFIG_PATH = broken_path
        cfg = plugin_main._read_dc_router_config()
        assert cfg["enabled"] is False
        assert cfg["fallback_on_error"] is True
        print("  ✅ JSON 损坏时安全默认 enabled=false")
    finally:
        plugin_main._DC_ROUTER_CONFIG_PATH = original_path
        os.unlink(broken_path)


def test_default_cmd_config_contains_dc_router_providers() -> None:
    """默认配置必须包含 dc-router 正式接管会用到的 provider ID。"""
    cfg_path = Path(_DC_AGENT_ROOT) / "data" / "cmd_config.json"
    with cfg_path.open(encoding="utf-8-sig") as f:
        data = json.load(f)

    provider_ids = {
        str(provider.get("id"))
        for provider in data.get("provider", [])
        if isinstance(provider, dict) and provider.get("enable", True)
    }
    required = {
        "aihubmix/qwen3.6-flash",
        "aihubmix/deepseek-v4-pro",
        "aihubmix/gemini-3.5-flash",
        "aihubmix/grok-4.3",
        "aihubmix/claude-sonnet-4-6",
        "aihubmix/claude-opus-4-7",
        "aihubmix/claude-opus-4-8",
        "codex/gpt-5.5-xhigh",
    }
    missing = required - provider_ids
    assert not missing, f"默认 cmd_config 缺少 dc-router provider: {sorted(missing)}"
    print("  ✅ 默认 cmd_config 包含 dc-router DIRECT provider")


# ─────────────────── 2. DCRouter 决策测试 ───────────────────


@pytest.mark.asyncio
async def test_router_basic_intents() -> None:
    """DCRouter.decide() business 路径对典型 case 返回合理意图。"""
    from dc_router import DCRouter

    dc_router = DCRouter()

    cases = [
        ("你好", "casual", "direct"),
        ("#前置 先帮我理一下这个需求", "work_preflight", "direct"),
        ("帮我写一段端午客户问候话术", "creative", "direct"),
        ("#深度 帮我分析五菱新能源的策略", "deep_insight", "direct"),
        ("今天行业有什么热点", "realtime", "direct"),
        ("我的 Python 脚本报错", "simple_code", "direct"),
        ("#创意 帮我写五菱端午营销文案", "creative", "direct"),
        ("#洞察 分析五菱新能源用户洞察", "insight", "direct"),
        ("舆情危机要怎么应对", "public_opinion", "direct"),
    ]
    for text, expected_intent, expected_depth in cases:
        decision = await dc_router.decide(text)
        assert decision.intent == expected_intent, (
            f"'{text}' 期望 intent={expected_intent}，实际 {decision.intent}"
        )
        assert decision.depth == expected_depth, (
            f"'{text}' 期望 depth={expected_depth}，实际 {decision.depth}"
        )
        print(f"  ✅ {text[:24]:<24} → {decision.intent} ({decision.depth})")


@pytest.mark.asyncio
async def test_router_deep_creative_uses_claude_opus_47() -> None:
    """Deep creative should use the current AIHubMix Claude Opus 4.7 route."""
    from dc_router import DCRouter, MessageEnvelope

    dc_router = DCRouter()
    decision = await dc_router.decide(
        MessageEnvelope(
            text="帮我做一份完整营销方案",
            metadata={"platform_id": "巅池-Agent小助手"},
        )
    )

    assert decision.intent == "deep_creative"
    assert decision.provider_id == "aihubmix/claude-opus-4-7"
    assert decision.target_model == "claude-opus-4-7"
    assert decision.resource_keys == ()
    print("  ✅ complete deep marketing task → AIHubMix Claude Opus 4.7")


@pytest.mark.asyncio
async def test_router_deep_insight_uses_claude_opus_48() -> None:
    """Deep insight should use the current AIHubMix Claude Opus 4.8 route."""
    from dc_router import DCRouter, MessageEnvelope

    dc_router = DCRouter()
    decision = await dc_router.decide(
        MessageEnvelope(
            text="#深度 品牌战略分析",
            metadata={"platform_id": "巅池-Agent小助手"},
        )
    )

    assert decision.intent == "deep_insight"
    assert decision.provider_id == "aihubmix/claude-opus-4-8"
    assert decision.target_model == "claude-opus-4-8"
    assert decision.resource_keys == ()
    print("  ✅ deep_insight → AIHubMix Claude Opus 4.8")


@pytest.mark.asyncio
async def test_router_feishu_wiki_link_uses_aihubmix_flash() -> None:
    """Feishu document links should be handled by AIHubMix Gemini 3.5 Flash."""
    from dc_router import DCRouter, MessageEnvelope

    dc_router = DCRouter()
    decision = await dc_router.decide(
        MessageEnvelope(
            text="https://o0ain5w98jh.feishu.cn/wiki/NJXowzJCtimtiXkx02mcoaOXngd",
            metadata={"platform_id": "巅池-Agent小助手"},
        )
    )

    assert decision.intent == "multimodal"
    assert decision.provider_id == "aihubmix/gemini-3.5-flash"
    assert decision.target_model == "gemini-3.5-flash"
    assert decision.source == "document_link"
    print("  ✅ Feishu wiki link → AIHubMix Gemini 3.5 Flash")


def test_deep_creative_and_insight_use_aihubmix_claude_without_queue() -> None:
    """Deep creative and insight should no longer require queued CLI resources."""
    from dc_router.provider_map import DEFAULT_PROVIDER_MAP
    from dc_router.taxonomy import RouterIntent

    creative = DEFAULT_PROVIDER_MAP[RouterIntent.DEEP_CREATIVE]
    insight = DEFAULT_PROVIDER_MAP[RouterIntent.DEEP_INSIGHT]

    assert creative.provider_id == "aihubmix/claude-opus-4-7"
    assert insight.provider_id == "aihubmix/claude-opus-4-8"
    assert creative.requires_queue is False
    assert insight.requires_queue is False
    assert creative.resource_keys == ()
    assert insight.resource_keys == ()
    print("  ✅ deep_creative / deep_insight → AIHubMix Claude, no queue")


@pytest.mark.asyncio
async def test_router_ops_intents() -> None:
    """DevOps platform 默认走 ops 路由表，且统一使用 Codex CLI gpt-5.4。"""
    from dc_router import DCRouter, MessageEnvelope

    dc_router = DCRouter()
    cases = [
        ("Hermes 状态如何", "system_status"),
        ("当前队列里有几个任务", "queue_status"),
        ("看看 aihubmix 用量怎么样", "quota_gate_view"),
        ("traceback 帮我看下错在哪", "error_debug"),
        ("写个 shell 脚本统计日志", "code_ops"),
        ("帮我重启服务", "deployment_ops"),
        ("#队列", "queue_status"),
        ("#部署 上线新版本", "deployment_ops"),
        ("呃这是什么", "ops_fallback"),
    ]
    for text, expected_intent in cases:
        envelope = MessageEnvelope(
            text=text,
            metadata={"platform_id": "巅池-技术（DevOps）"},
        )
        decision = await dc_router.decide(envelope)
        assert decision.intent == expected_intent, (
            f"'{text}' 期望 ops intent={expected_intent}，实际 {decision.intent}"
        )
        assert decision.metadata.get("router_mode") == "ops"
        assert decision.depth == "direct"
        assert decision.provider_id == "cli/codex/gpt-5.4"
        assert decision.target_model == "gpt-5.4"
        print(
            f"  ✅ {text[:18]:<18} → ops/{decision.intent}  "
            f"provider={decision.provider_id}"
        )


@pytest.mark.asyncio
async def test_devops_platform_uses_ops_router_by_default() -> None:
    """生产默认：「巅池-技术（DevOps）」platform 走 ops 路由。"""
    from dc_router import DCRouter, MessageEnvelope

    dc_router = DCRouter()
    envelope = MessageEnvelope(
        text="Hermes 状态如何",
        metadata={"platform_id": "巅池-技术（DevOps）"},
    )
    decision = await dc_router.decide(envelope)
    assert decision.metadata.get("router_mode") == "ops"
    assert decision.intent == "system_status"
    assert decision.provider_id == "cli/codex/gpt-5.4"
    print(
        f"  ✅ DevOps platform 默认走 ops → {decision.intent} ({decision.provider_id})"
    )


@pytest.mark.asyncio
async def test_router_business_unaffected_by_ops_keywords() -> None:
    """在 business platform 上发 ops 关键词，应该走 business 路由。"""
    from dc_router import DCRouter, MessageEnvelope

    dc_router = DCRouter()
    envelope = MessageEnvelope(
        text="看看队列里几个任务",
        metadata={"platform_id": "巅池-Agent小助手"},
    )
    decision = await dc_router.decide(envelope)
    assert decision.intent != "queue_status", (
        f"business 不应用 ops 意图 queue_status，实际 {decision.intent}"
    )
    assert decision.metadata.get("router_mode") != "ops"
    print(f"  ✅ business platform 上 ops 关键词不串扰 → {decision.intent}")


# ─────────────────── 3. QuotaGate 端到端 ───────────────────


@pytest.mark.asyncio
async def test_quota_gate_lifecycle() -> None:
    """QuotaGate admit → complete → 再 admit (冷却) → fail。"""
    from dc_quota_runtime import get_quota_gate, reset_quota_gate_for_test
    from harness import AdmissionMode, QuotaRequest

    reset_quota_gate_for_test()

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        test_db = f.name

    try:
        gate = await get_quota_gate(test_db)

        # admit 1: 应 RUN_NOW
        req = QuotaRequest(
            primary_resource_key="codex_oauth_xhigh",
            resource_keys=("codex_oauth_xhigh",),
            payload={"test": True},
            requested_by="test-1",
            session_id="s1",
        )
        d1 = await gate.admit(req)
        assert d1.mode == AdmissionMode.RUN_NOW
        print("  ✅ admit 1 (空闲) → RUN_NOW")

        # admit 2: 同资源，应 QUEUED
        d2 = await gate.admit(req)
        assert d2.mode == AdmissionMode.QUEUED
        assert d2.queue_position >= 1
        pending_jobs = await gate.list_pending_jobs()
        assert any(job.job_id == d2.job.job_id for job in pending_jobs)
        print(f"  ✅ admit 2 (占用) → QUEUED, pos={d2.queue_position}")

        # complete 1: 资源进冷却
        await gate.complete(d1.job.job_id, result={"out": "ok"})

        # admit 3 (冷却中): 仍 QUEUED
        d3 = await gate.admit(req)
        assert d3.mode == AdmissionMode.QUEUED
        print("  ✅ admit 3 (冷却中) → QUEUED")

        # 用另一资源 + fail() 路径
        d4 = await gate.admit(
            QuotaRequest(
                primary_resource_key="claude_oauth_sonnet_4_6",
                resource_keys=("claude_oauth_sonnet_4_6",),
                payload={},
            )
        )
        assert d4.mode == AdmissionMode.RUN_NOW
        await gate.fail(d4.job.job_id, error="simulated", retry_after_seconds=60)
        print("  ✅ fail() 路径 OK")
    finally:
        reset_quota_gate_for_test()
        if os.path.exists(test_db):
            os.unlink(test_db)


@pytest.mark.asyncio
async def test_quota_gate_start_pending_job() -> None:
    """A pending job can be recovered into RUNNING once cooldown clears."""
    from harness import AdmissionMode, QuotaGate, QuotaRequest, QueueStatus
    from harness.resources import ResourceConfig

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        test_db = f.name

    try:
        gate = QuotaGate(
            test_db,
            resource_configs={
                "test_resource": ResourceConfig(
                    key="test_resource",
                    cooldown_after_completion_seconds=0,
                    estimated_run_seconds=1,
                )
            },
        )
        req = QuotaRequest(
            primary_resource_key="test_resource",
            resource_keys=("test_resource",),
            payload={"provider_id": "codex/gpt-5.5-xhigh"},
        )
        d1 = await gate.admit(req)
        d2 = await gate.admit(req)
        assert d1.mode == AdmissionMode.RUN_NOW
        assert d2.mode == AdmissionMode.QUEUED

        started_while_busy = await gate.start_pending_job(d2.job.job_id)
        assert started_while_busy is None

        await gate.complete(d1.job.job_id, cooldown_seconds=0)
        started = await gate.start_pending_job(d2.job.job_id)
        assert started is not None
        assert started.status == QueueStatus.RUNNING
        assert started.job_id == d2.job.job_id

        duplicate_start = await gate.start_pending_job(d2.job.job_id)
        assert duplicate_start is None
        print("  ✅ pending job recovery: QUEUED → RUNNING once resource is free")
    finally:
        if os.path.exists(test_db):
            os.unlink(test_db)


@pytest.mark.asyncio
async def test_quota_gate_cancel_pending_job() -> None:
    """Cancel pending jobs without releasing resources held by a running job."""
    from harness import AdmissionMode, QuotaGate, QuotaRequest, QueueStatus
    from harness.resources import ResourceConfig

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        test_db = f.name

    try:
        gate = QuotaGate(
            test_db,
            resource_configs={
                "test_resource": ResourceConfig(
                    key="test_resource",
                    cooldown_after_completion_seconds=0,
                    estimated_run_seconds=1,
                )
            },
        )
        req = QuotaRequest(
            primary_resource_key="test_resource",
            resource_keys=("test_resource",),
            payload={"provider_id": "codex/gpt-5.5-xhigh"},
        )
        running = await gate.admit(req)
        pending = await gate.admit(req)
        assert running.mode == AdmissionMode.RUN_NOW
        assert pending.mode == AdmissionMode.QUEUED

        cancelled = await gate.cancel_pending_job(
            pending.job.job_id,
            "routed to oauth fallback",
        )
        assert cancelled is True
        cancelled_job = await gate.store.get_job(pending.job.job_id)
        assert cancelled_job is not None
        assert cancelled_job.status == QueueStatus.CANCELLED

        duplicate = await gate.start_pending_job(pending.job.job_id)
        assert duplicate is None

        still_busy = await gate.admit(req)
        assert still_busy.mode == AdmissionMode.QUEUED
        await gate.complete(running.job.job_id, cooldown_seconds=0)
        print("  ✅ cancel pending job: 不释放 running 资源，避免重复恢复执行")
    finally:
        if os.path.exists(test_db):
            os.unlink(test_db)


# ─────────────────── 4. Adapter 边界层 ───────────────────


@pytest.mark.asyncio
async def test_adapter_route_signature() -> None:
    """dc_router_adapter 导出的函数签名 + lazy import 不报错。"""
    from dc_router_adapter import (
        apply_decision,
        event_to_envelope,
        route_via_dc_router,
    )

    assert callable(event_to_envelope)
    assert callable(apply_decision)
    assert callable(route_via_dc_router)
    print(
        "  ✅ adapter 三个函数导出 OK (event_to_envelope / apply_decision / route_via_dc_router)"
    )


def test_parse_cli_provider_sonnet_high() -> None:
    """CLI provider parser should split generic Claude high correctly."""
    from dc_router_adapter import _parse_cli_provider

    assert _parse_cli_provider("cli/claude-sonnet-high") == (
        "claude",
        "claude-sonnet",
        "high",
    )
    print("  ✅ parse cli provider: Claude high")


def test_parse_cli_provider_codex() -> None:
    """CLI provider parser should split Codex CLI provider ids correctly."""
    from dc_router_adapter import _parse_cli_provider

    assert _parse_cli_provider("cli/codex/gpt-5.4") == (
        "codex",
        "gpt-5.4",
        None,
    )
    print("  ✅ parse cli provider: Codex gpt-5.4")


def test_parse_cli_provider_antigravity() -> None:
    """CLI provider parser should split Antigravity provider ids correctly."""
    from dc_router_adapter import _parse_cli_provider

    assert _parse_cli_provider("cli/antigravity/gemini-3.5-flash") == (
        "antigravity",
        "gemini-3.5-flash",
        None,
    )
    print("  ✅ parse cli provider: Antigravity Gemini 3.5 Flash")


def test_event_to_envelope_marks_video_attachment() -> None:
    """Video attachments should trigger multimodal preprocessing in dc-router."""
    from astrbot.api.message_components import Video
    from dc_router_adapter import event_to_envelope
    from dc_router import AttachmentKind

    event = _make_mock_event("请识别这个视频", "巅池-Agent小助手")
    event.message_obj.message = [Video(file="/tmp/mock.mp4", path="/tmp/mock.mp4")]

    envelope = event_to_envelope(event)

    assert AttachmentKind.VIDEO in envelope.attachment_kinds
    assert envelope.has_attachments is True
    print("  ✅ adapter: video attachment → multimodal envelope")


# ─────────────────── 5. CLI runner ───────────────────


@pytest.mark.asyncio
async def test_cli_runner_gemini_mock_success() -> None:
    """CliRunner.run_gemini() builds a safe Gemini command and parses JSON."""
    import cli_runner
    from cli_runner import CliRunner

    captured: dict = {}

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            payload = {
                "response": "你好呀！",
                "stats": {"models": {"gemini-3.1-pro-preview": {}}},
            }
            return json.dumps(payload).encode(), b""

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return FakeProcess()

    original_exec = cli_runner.asyncio.create_subprocess_exec
    try:
        cli_runner.asyncio.create_subprocess_exec = fake_exec
        result = await CliRunner().run_gemini(
            "你好",
            model="gemini-3.1-pro-preview",
            timeout=1,
        )
    finally:
        cli_runner.asyncio.create_subprocess_exec = original_exec

    assert result.ok is True
    assert result.text == "你好呀！"
    args = captured["args"]
    assert args[0] == "gemini"
    assert "-p" in args
    assert "--output-format" in args
    assert "json" in args
    assert "--approval-mode" in args
    assert "plan" in args
    assert captured["kwargs"]["cwd"] == tempfile.gettempdir()
    print("  ✅ CliRunner Gemini mock: 参数安全 + JSON 解析 OK")


@pytest.mark.asyncio
async def test_cli_runner_claude_mock_success() -> None:
    """CliRunner.run_claude() forces JSON and safe read-only tools."""
    import cli_runner
    from cli_runner import CliRunner

    captured: dict = {}

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            payload = {
                "result": "深度结果",
                "modelUsage": {"claude-sonnet": {"outputTokens": 123}},
            }
            return json.dumps(payload).encode(), b""

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return FakeProcess()

    original_exec = cli_runner.asyncio.create_subprocess_exec
    try:
        cli_runner.asyncio.create_subprocess_exec = fake_exec
        result = await CliRunner().run_claude(
            "请深度分析",
            model="claude-sonnet",
            effort="xhigh",
            timeout=1,
        )
    finally:
        cli_runner.asyncio.create_subprocess_exec = original_exec

    assert result.ok is True
    assert result.text == "深度结果"
    args = captured["args"]
    assert Path(args[0]).name in {"Claude", "claude"}
    assert "--model" in args
    assert "claude-sonnet" in args
    assert "--effort" in args
    assert "xhigh" in args
    assert "--max-turns" in args
    turns_idx = args.index("--max-turns")
    assert args[turns_idx + 1] == "3"
    tools_idx = args.index("--allowed-tools")
    assert args[tools_idx + 1] == "Read,Glob,Grep,Skill"
    assert "--tools" not in args
    assert "--add-dir" in args
    print("  ✅ CliRunner Claude mock: read-only tools + JSON 解析 OK")


@pytest.mark.asyncio
async def test_cli_runner_codex_mock_success() -> None:
    """CliRunner.run_codex() runs Codex CLI in read-only one-shot mode."""
    import cli_runner
    from cli_runner import CliRunner

    captured: dict = {}

    class FakeProcess:
        returncode = 0

        async def communicate(self, stdin_b=None):
            captured["stdin"] = stdin_b.decode("utf-8")
            args = captured["args"]
            output_path = Path(args[args.index("--output-last-message") + 1])
            output_path.write_text("Codex result", encoding="utf-8")
            return b"codex stdout", b""

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return FakeProcess()

    original_exec = cli_runner.asyncio.create_subprocess_exec
    try:
        cli_runner.asyncio.create_subprocess_exec = fake_exec
        result = await CliRunner().run_codex(
            "解释这个 traceback",
            model="gpt-5.4",
            timeout=1,
        )
    finally:
        cli_runner.asyncio.create_subprocess_exec = original_exec

    assert result.ok is True
    assert result.text == "Codex result"
    assert captured["stdin"] == "解释这个 traceback"
    args = captured["args"]
    assert args[0:2] == ("codex", "exec")
    assert "--skip-git-repo-check" in args
    assert "--sandbox" in args
    assert args[args.index("--sandbox") + 1] == "read-only"
    assert "--model" in args
    assert args[args.index("--model") + 1] == "gpt-5.4"
    assert args[-1] == "-"
    print("  ✅ CliRunner Codex mock: read-only exec + 输出文件解析 OK")


@pytest.mark.asyncio
async def test_cli_runner_antigravity_mock_success() -> None:
    """CliRunner.run_antigravity() uses a configurable headless command."""
    import cli_runner
    from cli_runner import CliRunner

    captured: dict = {}

    class FakeProcess:
        returncode = 0

        async def communicate(self, stdin_b=None):
            captured["stdin"] = stdin_b.decode("utf-8")
            payload = {"response": "Antigravity result"}
            return json.dumps(payload).encode(), b""

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return FakeProcess()

    original_exec = cli_runner.asyncio.create_subprocess_exec
    original_bin = os.environ.get("DC_ANTIGRAVITY_CLI_BIN")
    original_args = os.environ.get("DC_ANTIGRAVITY_CLI_ARGS")
    try:
        os.environ["DC_ANTIGRAVITY_CLI_BIN"] = "agy"
        os.environ["DC_ANTIGRAVITY_CLI_ARGS"] = (
            "--print {prompt} --print-timeout {timeout_seconds}s"
        )
        cli_runner.asyncio.create_subprocess_exec = fake_exec
        result = await CliRunner().run_antigravity(
            "请看一下这张图",
            model="gemini-3.5-flash",
            timeout=1,
            attachment_paths=["/tmp/a.png"],
        )
    finally:
        cli_runner.asyncio.create_subprocess_exec = original_exec
        if original_bin is None:
            os.environ.pop("DC_ANTIGRAVITY_CLI_BIN", None)
        else:
            os.environ["DC_ANTIGRAVITY_CLI_BIN"] = original_bin
        if original_args is None:
            os.environ.pop("DC_ANTIGRAVITY_CLI_ARGS", None)
        else:
            os.environ["DC_ANTIGRAVITY_CLI_ARGS"] = original_args

    assert result.ok is True
    assert result.text == "Antigravity result"
    args = captured["args"]
    assert args[0] == "agy"
    assert "--print" in args
    assert "--print-timeout" in args
    prompt_arg = args[args.index("--print") + 1]
    assert "<local_attachments>" in prompt_arg
    assert "/tmp/a.png" in prompt_arg
    print("  ✅ CliRunner Antigravity mock: agy print 参数 + JSON 解析 OK")


def test_antigravity_location_warning_is_nonfatal_with_text(monkeypatch) -> None:
    """Agy may log account eligibility warnings while still returning text."""
    import cli_runner
    from cli_runner import CliRunner

    class FakeChild:
        exitstatus = 0

        def __init__(self):
            self._chunks = ["正常回答"]

        def read_nonblocking(self, size, timeout):
            if self._chunks:
                return self._chunks.pop(0)
            raise cli_runner.pexpect.exceptions.EOF("done")

        def isalive(self):
            return False

    def fake_spawn(*args, **kwargs):
        return FakeChild()

    log_path = Path(tempfile.mkdtemp()) / "agy.log"
    log_path.write_text(
        "Account ineligible: Your current account is not eligible for Antigravity, "
        "because it is not currently available in your location.",
        encoding="utf-8",
    )
    monkeypatch.setattr(cli_runner.pexpect, "spawn", fake_spawn)
    result = CliRunner()._run_antigravity_pty(
        "agy",
        ["--print", "hello"],
        timeout=1,
        log_path=log_path,
    )
    assert result.ok is True
    assert result.text == "正常回答"


def test_antigravity_health_records_history_events(
    isolated_antigravity_health,
) -> None:
    """Antigravity health writes bounded event history for flap diagnosis."""
    health = isolated_antigravity_health

    success_state = health.mark_antigravity_success(elapsed_sec=1.2345)
    assert success_state["status"] == "healthy"

    failure_state = health.mark_antigravity_failure(
        error_code="unsupported_location",
        error="agy account/location is not eligible",
    )
    assert failure_state["last_error_code"] == "unsupported_location"

    health.record_antigravity_circuit_fallback(
        reason="unsupported_location",
        state=failure_state,
    )
    summary = health.summarize_antigravity_history()

    assert summary["event_counts"] == {
        "success": 1,
        "failure": 1,
        "circuit_fallback": 1,
    }
    assert summary["reason_counts"]["unsupported_location"] == 2
    recent_events = summary["recent_events"]
    assert recent_events[0]["event"] == "success"
    assert recent_events[0]["elapsed_sec"] == 1.234
    assert recent_events[1]["event"] == "failure"
    assert recent_events[2]["event"] == "circuit_fallback"
    print("  ✅ Antigravity health history: success/failure/fallback 事件可统计")


@pytest.mark.asyncio
async def test_cli_runner_timeout() -> None:
    """CliRunner returns timeout instead of hanging the event loop."""
    import cli_runner
    from cli_runner import CliRunner

    class SlowProcess:
        returncode = None
        killed = False

        async def communicate(self):
            if self.killed:
                return b"", b""
            await asyncio.sleep(10)
            return b"", b""

        def kill(self):
            self.killed = True

    slow_process = SlowProcess()

    async def fake_exec(*args, **kwargs):
        return slow_process

    original_exec = cli_runner.asyncio.create_subprocess_exec
    try:
        cli_runner.asyncio.create_subprocess_exec = fake_exec
        result = await CliRunner().run_gemini("你好", timeout=0.01)
    finally:
        cli_runner.asyncio.create_subprocess_exec = original_exec

    assert result.ok is False
    assert result.error_code == "timeout"
    assert slow_process.killed is True
    print("  ✅ CliRunner timeout: 超时会 kill 并返回 timeout")


# ─────────────────── 6. 端到端集成 (mock AstrBot 全链路) ───────────────────


def _make_mock_provider(provider_id: str):
    """构造一个有 .meta().id 属性的 mock provider。"""
    from unittest.mock import MagicMock

    p = MagicMock()
    meta_obj = MagicMock()
    meta_obj.id = provider_id
    p.meta = MagicMock(return_value=meta_obj)
    return p


def _make_mock_event(text: str, platform_id: str):
    """构造一个有 plugin route() 需要的属性的 mock event。"""
    from unittest.mock import MagicMock

    event = MagicMock()
    event.message_str = text
    event.get_platform_id = MagicMock(return_value=platform_id)
    event.unified_msg_origin = f"lark:{platform_id}:test-session"
    event.get_sender_id = MagicMock(return_value="test-user")
    event.message_obj = MagicMock()
    event.message_obj.message_str = text
    event.message_obj.message = []  # 无附件
    event.set_extra = MagicMock()
    event.should_call_llm = MagicMock()
    event.set_result = MagicMock()
    event.track_temporary_local_file = MagicMock()
    event.is_at_or_wake_command = False
    event.get_group_id = MagicMock(return_value="")
    return event


def _make_mock_ctx(
    available_provider_ids: list[str], provider_overrides: dict | None = None
):
    """构造 mock context: get_all_providers + provider_manager.set_provider(async)。"""
    from unittest.mock import AsyncMock, MagicMock

    ctx = MagicMock()
    providers_by_id = {pid: _make_mock_provider(pid) for pid in available_provider_ids}
    if provider_overrides:
        providers_by_id.update(provider_overrides)
    ctx.get_all_providers = MagicMock(
        return_value=[_make_mock_provider(pid) for pid in available_provider_ids]
    )
    ctx.get_provider_by_id = MagicMock(side_effect=lambda pid: providers_by_id.get(pid))
    ctx.get_config = MagicMock(return_value={"provider_settings": {}})
    ctx.provider_manager = MagicMock()
    ctx.provider_manager.set_provider = AsyncMock(return_value=None)
    ctx.send_message = AsyncMock(return_value=True)
    ctx.conversation_manager = None
    ctx.harness_engine = None
    ctx.harness_store = None
    ctx.kb_manager = None
    return ctx


@pytest.mark.asyncio
async def test_e2e_local_casual_ack_does_not_call_provider() -> None:
    """Short acknowledgement chatter should stop locally and avoid Agy/LLM usage."""
    from dc_router_adapter import route_via_dc_router

    ctx = _make_mock_ctx(["aihubmix/qwen3.6-flash"])
    event = _make_mock_event("嗯嗯", "巅池-Agent小助手")

    handled = await route_via_dc_router(ctx, event, dry_run=False)

    assert handled is True
    ctx.provider_manager.set_provider.assert_not_called()
    event.should_call_llm.assert_called_once_with(False)
    event.set_result.assert_called_once()
    event.set_extra.assert_any_call("llm_router_provider", "local/no_llm")
    print("  ✅ local casual ack: no provider / no agy")


@pytest.mark.asyncio
async def test_e2e_business_route() -> None:
    """端到端 (business): 飞书消息 → adapter → DCRouter → set_provider。

    dry_run=False（正式接管模式），验证 set_provider 真被调用。
    """
    import grok_worker
    from cli_runner import CliResult
    from dc_router_adapter import route_via_dc_router

    class FakeGrokWorker:
        async def ask_public_opinion(self, prompt):
            return CliResult(error_code="test_fallback", error="offline test")

    cases = [
        ("你好", "aihubmix/qwen3.6-flash"),
        ("帮我写一封邮件", "aihubmix/qwen3.6-flash"),
        ("帮我写一段端午客户问候话术", "aihubmix/deepseek-v4-pro"),
        ("舆情危机要怎么应对", "aihubmix/grok-4.3"),
    ]
    available = [
        "aihubmix/qwen3.6-flash",
        "aihubmix/deepseek-v4-pro",
        "aihubmix/gemini-3.5-flash",
        "aihubmix/grok-4.3",
        "aihubmix/claude-opus-4-7",
    ]

    original_get_grok_worker = grok_worker.get_grok_worker
    try:
        grok_worker.get_grok_worker = lambda: FakeGrokWorker()
        for text, expected_provider in cases:
            ctx = _make_mock_ctx(available)
            event = _make_mock_event(text, "巅池-Agent小助手")
            handled = await route_via_dc_router(ctx, event, dry_run=False)
            assert handled is True, f"'{text}' 期望 handled=True"
            ctx.provider_manager.set_provider.assert_called_once()
            actual_provider = ctx.provider_manager.set_provider.call_args.kwargs[
                "provider_id"
            ]
            assert actual_provider == expected_provider, (
                f"'{text}' 期望 {expected_provider}，实际 {actual_provider}"
            )
            umo = ctx.provider_manager.set_provider.call_args.kwargs["umo"]
            assert umo == event.unified_msg_origin
            print(
                f"  ✅ business e2e (dry_run=False): {text[:20]:<20} → set_provider({actual_provider})"
            )
    finally:
        grok_worker.get_grok_worker = original_get_grok_worker


@pytest.mark.asyncio
async def test_e2e_realtime_and_fallback_antigravity_success_route(
    isolated_antigravity_health,
) -> None:
    """realtime/fallback should use Agy first, not direct AIHubMix provider switch."""
    import antigravity_health
    import cli_runner
    import dc_quota_runtime
    from cli_runner import CliResult
    from dc_router_adapter import route_via_dc_router
    from harness import AdmissionDecision, AdmissionMode, QueueJob, QueueStatus

    captured: dict[str, list[dict]] = {"agy": []}

    class FakeGate:
        async def admit(self, request):
            return AdmissionDecision(
                mode=AdmissionMode.RUN_NOW,
                job=QueueJob(
                    job_id=f"agy-{len(captured['agy'])}",
                    primary_resource_key=request.primary_resource_key,
                    resource_keys=request.resource_keys,
                    status=QueueStatus.RUNNING,
                ),
            )

        async def complete(self, job_id, result=None, cooldown_seconds=None):
            return None

    class FakeCliRunner:
        async def run_antigravity(self, prompt, *, model, timeout):
            captured["agy"].append(
                {"prompt": prompt, "model": model, "timeout": timeout}
            )
            return CliResult(text="Agy handled this light route.", elapsed_sec=0.8)

    async def fake_get_quota_gate(db_path=None):
        return FakeGate()

    original_allowed = antigravity_health.antigravity_allowed
    original_runner = cli_runner.CliRunner
    original_get_gate = dc_quota_runtime.get_quota_gate
    try:
        antigravity_health.antigravity_allowed = lambda: (True, "", {})
        cli_runner.CliRunner = lambda *args, **kwargs: FakeCliRunner()
        dc_quota_runtime.get_quota_gate = fake_get_quota_gate

        for text in ("今天行业有什么热点", "呃这个我说不清楚"):
            ctx = _make_mock_ctx(["aihubmix/gemini-3.5-flash"])
            event = _make_mock_event(text, "巅池-Agent小助手")
            handled = await route_via_dc_router(ctx, event, dry_run=False)

            assert handled is True
            ctx.provider_manager.set_provider.assert_not_called()
            event.should_call_llm.assert_called_once_with(False)
            event.set_result.assert_called_once()
    finally:
        antigravity_health.antigravity_allowed = original_allowed
        cli_runner.CliRunner = original_runner
        dc_quota_runtime.get_quota_gate = original_get_gate

    assert [item["model"] for item in captured["agy"]] == [
        "gemini-3.5-flash",
        "gemini-3.5-flash",
    ]
    assert "轻量实时信息" in captured["agy"][0]["prompt"]
    assert "不明确但不是垃圾闲聊" in captured["agy"][1]["prompt"]
    print("  ✅ realtime/fallback e2e: Agy first, no direct AIHubMix switch")


@pytest.mark.asyncio
async def test_e2e_simple_code_codex_cli_route() -> None:
    """business simple_code: direct Codex CLI answers and stops AstrBot LLM."""
    import cli_runner
    from cli_runner import CliResult
    from dc_router_adapter import route_via_dc_router

    captured: dict = {}

    class FakeCliRunner:
        def __init__(self, *args, **kwargs):
            captured["runner_init"] = {"args": args, "kwargs": kwargs}

        async def run_codex(self, prompt, *, model, timeout):
            captured["codex"] = {
                "prompt": prompt,
                "model": model,
                "timeout": timeout,
            }
            return CliResult(text="这是 Codex CLI 的代码解释。", elapsed_sec=1.2)

    original_runner = cli_runner.CliRunner
    try:
        cli_runner.CliRunner = FakeCliRunner
        ctx = _make_mock_ctx([])
        event = _make_mock_event("我的 Python 脚本报错", "巅池-Agent小助手")
        handled = await route_via_dc_router(ctx, event, dry_run=False)
    finally:
        cli_runner.CliRunner = original_runner

    assert handled is True
    ctx.provider_manager.set_provider.assert_not_called()
    event.should_call_llm.assert_called_once_with(False)
    event.set_result.assert_called_once()
    assert captured["codex"]["model"] == "gpt-5.4"
    assert captured["codex"]["timeout"] == 300
    assert Path(captured["runner_init"]["kwargs"]["cwd"]) == Path(_DC_AGENT_ROOT)
    assert "Python 脚本报错" in captured["codex"]["prompt"]
    result_text = event.set_result.call_args.args[0].get_plain_text()
    assert "Codex CLI" in result_text
    print("  ✅ simple_code e2e: cli/codex/gpt-5.4 → direct answer")


@pytest.mark.asyncio
async def test_e2e_work_preflight_antigravity_success_route(
    isolated_antigravity_health,
) -> None:
    """工作前置: 飞书消息 → dc-router → Antigravity CLI direct answer."""
    import cli_runner
    import dc_quota_runtime
    import antigravity_health
    from cli_runner import CliResult
    from dc_router_adapter import route_via_dc_router
    from harness import AdmissionDecision, AdmissionMode, QueueJob, QueueStatus

    captured: dict = {}

    class FakeGate:
        async def admit(self, request):
            captured["quota_request"] = request
            return AdmissionDecision(
                mode=AdmissionMode.RUN_NOW,
                job=QueueJob(
                    job_id="agy-run-now",
                    primary_resource_key=request.primary_resource_key,
                    resource_keys=request.resource_keys,
                    status=QueueStatus.RUNNING,
                ),
            )

        async def complete(self, job_id, result=None, cooldown_seconds=None):
            captured["complete"] = {
                "job_id": job_id,
                "result": result,
                "cooldown_seconds": cooldown_seconds,
            }

    class FakeCliRunner:
        def __init__(self, *args, **kwargs):
            captured["runner_init"] = {"args": args, "kwargs": kwargs}

        async def run_antigravity(self, prompt, *, model, timeout):
            captured["agy"] = {"prompt": prompt, "model": model, "timeout": timeout}
            return CliResult(text="你好，我是巅池-Agent小助手。", elapsed_sec=2.4)

    async def fake_get_quota_gate(db_path=None):
        return FakeGate()

    original_allowed = antigravity_health.antigravity_allowed
    original_runner = cli_runner.CliRunner
    original_get_gate = dc_quota_runtime.get_quota_gate
    try:
        antigravity_health.antigravity_allowed = lambda: (True, "", {})
        cli_runner.CliRunner = FakeCliRunner
        dc_quota_runtime.get_quota_gate = fake_get_quota_gate
        ctx = _make_mock_ctx(["aihubmix/gemini-3.5-flash"])
        event = _make_mock_event("#前置 先帮我理一下这个需求", "巅池-Agent小助手")
        handled = await route_via_dc_router(ctx, event, dry_run=False)
    finally:
        antigravity_health.antigravity_allowed = original_allowed
        cli_runner.CliRunner = original_runner
        dc_quota_runtime.get_quota_gate = original_get_gate

    assert handled is True
    assert captured["quota_request"].primary_resource_key == "antigravity_cli_flash"
    assert captured["agy"]["model"] == "gemini-3.5-flash"
    assert captured["agy"]["timeout"] == 90
    assert captured["complete"]["job_id"] == "agy-run-now"
    ctx.provider_manager.set_provider.assert_not_called()
    event.should_call_llm.assert_called_once_with(False)
    event.set_result.assert_called_once()
    result_text = event.set_result.call_args.args[0].get_plain_text()
    assert "巅池-Agent小助手" in result_text
    print("  ✅ work_preflight e2e: #前置 → Antigravity CLI → direct answer")


@pytest.mark.asyncio
async def test_e2e_work_preflight_antigravity_failure_falls_back_to_aihubmix(
    isolated_antigravity_health,
) -> None:
    """工作前置: Antigravity CLI 失败时切回 AIHubMix Gemini 3.5 Flash."""
    import cli_runner
    import dc_quota_runtime
    import antigravity_health
    from cli_runner import CliResult
    from dc_router_adapter import route_via_dc_router
    from harness import AdmissionDecision, AdmissionMode, QueueJob, QueueStatus

    captured: dict = {}

    class FakeGate:
        async def admit(self, request):
            return AdmissionDecision(
                mode=AdmissionMode.RUN_NOW,
                job=QueueJob(
                    job_id="agy-failed",
                    primary_resource_key=request.primary_resource_key,
                    resource_keys=request.resource_keys,
                    status=QueueStatus.RUNNING,
                ),
            )

        async def fail(self, job_id, reason, retry_after_seconds=None):
            captured["fail"] = {
                "job_id": job_id,
                "reason": reason,
                "retry_after_seconds": retry_after_seconds,
            }

    class FakeCliRunner:
        async def run_antigravity(self, prompt, *, model, timeout):
            captured["agy"] = {"prompt": prompt, "model": model, "timeout": timeout}
            return CliResult(error_code="auth_required", error="agy requires OAuth")

    async def fake_get_quota_gate(db_path=None):
        return FakeGate()

    original_allowed = antigravity_health.antigravity_allowed
    original_runner = cli_runner.CliRunner
    original_get_gate = dc_quota_runtime.get_quota_gate
    try:
        antigravity_health.antigravity_allowed = lambda: (True, "", {})
        cli_runner.CliRunner = lambda *args, **kwargs: FakeCliRunner()
        dc_quota_runtime.get_quota_gate = fake_get_quota_gate
        ctx = _make_mock_ctx(["aihubmix/gemini-3.5-flash"])
        event = _make_mock_event("#前置 先帮我理一下这个需求", "巅池-Agent小助手")
        handled = await route_via_dc_router(ctx, event, dry_run=False)
    finally:
        antigravity_health.antigravity_allowed = original_allowed
        cli_runner.CliRunner = original_runner
        dc_quota_runtime.get_quota_gate = original_get_gate

    assert handled is True
    assert captured["fail"]["job_id"] == "agy-failed"
    ctx.provider_manager.set_provider.assert_called_once()
    actual_provider = ctx.provider_manager.set_provider.call_args.kwargs["provider_id"]
    assert actual_provider == "aihubmix/gemini-3.5-flash"
    event.set_extra.assert_any_call(
        "dc_router_antigravity_failure_fallback",
        {
            "provider_id": "cli/antigravity/gemini-3.5-flash",
            "fallback_provider_id": "aihubmix/gemini-3.5-flash",
            "error_code": "auth_required",
        },
    )
    print("  ✅ work_preflight fallback: Antigravity 失败 → AIHubMix Gemini 3.5 Flash")


@pytest.mark.asyncio
async def _legacy_e2e_casual_antigravity_queue_card_text() -> None:
    """20 人并发保护: agy 忙时展示真实排队数据和备用方案入口文案。"""
    import cli_runner
    import dc_quota_runtime
    from dc_router_adapter import route_via_dc_router
    from harness import AdmissionDecision, AdmissionMode, QueueJob, QueueStatus

    captured: dict = {}

    class FakeGate:
        async def admit(self, request):
            captured["quota_request"] = request
            return AdmissionDecision(
                mode=AdmissionMode.QUEUED,
                job=QueueJob(
                    job_id="agy-overflow",
                    primary_resource_key=request.primary_resource_key,
                    resource_keys=request.resource_keys,
                    status=QueueStatus.PENDING,
                ),
                queue_position=3,
                eta_at=9999999999,
            )

        async def start_pending_job(self, job_id):
            captured.setdefault("wait_attempts", 0)
            captured["wait_attempts"] += 1
            return None

    class FakeCliRunner:
        def __init__(self, *args, **kwargs):
            raise AssertionError("overflow fallback should not run agy")

    async def fake_get_quota_gate(db_path=None):
        return FakeGate()

    original_runner = cli_runner.CliRunner
    original_get_gate = dc_quota_runtime.get_quota_gate
    try:
        cli_runner.CliRunner = FakeCliRunner
        dc_quota_runtime.get_quota_gate = fake_get_quota_gate
        ctx = _make_mock_ctx(["aihubmix/gemini-3.5-flash"])
        event = _make_mock_event("你好", "巅池-Agent小助手")
        handled = await route_via_dc_router(ctx, event, dry_run=False)
    finally:
        cli_runner.CliRunner = original_runner
        dc_quota_runtime.get_quota_gate = original_get_gate

    assert handled is True
    ctx.provider_manager.set_provider.assert_not_called()
    event.should_call_llm.assert_called_once_with(False)
    event.set_result.assert_called_once()
    result_text = event.set_result.call_args.args[0].get_plain_text()
    assert "当前同时接待人数已经超过 9 人" in result_text
    assert "当前位置：第 3 位" in result_text
    assert "当前通道：巅巅小助手" in result_text
    assert "池池小助手" in result_text
    queue_extra = event.set_extra.call_args_list[-1].args[1]
    assert queue_extra["job_id"] == "agy-overflow"
    assert queue_extra["queue_position"] == 3
    assert queue_extra["fallback_channel_name"] == "池池小助手"
    assert captured["wait_attempts"] >= 1
    print("  ✅ casual queue: agy busy → true queue text + 池池小助手 option")


@pytest.mark.asyncio
async def test_antigravity_queue_card_action_cancels_job_and_switches_fallback() -> (
    None
):
    import dc_quota_runtime
    from dc_router_adapter import maybe_handle_antigravity_queue_card_action

    captured: dict = {}

    class FakeGate:
        async def cancel_pending_job(self, job_id, reason=""):
            captured["cancel"] = {"job_id": job_id, "reason": reason}
            return True

    async def fake_get_quota_gate(db_path=None):
        return FakeGate()

    original_get_gate = dc_quota_runtime.get_quota_gate
    try:
        dc_quota_runtime.get_quota_gate = fake_get_quota_gate
        ctx = _make_mock_ctx(["aihubmix/gemini-3.5-flash"])
        event = _make_mock_event(
            "__card_action__:"
            + json.dumps(
                {
                    "value": {
                        "source": "antigravity_queue_card",
                        "action": "use_fallback",
                        "job_id": "agy-overflow",
                        "fallback_provider_id": "aihubmix/gemini-3.5-flash",
                        "original_prompt": "你好",
                    }
                },
                ensure_ascii=False,
            ),
            "巅池-Agent小助手",
        )
        event.is_card_action = True
        handled = await maybe_handle_antigravity_queue_card_action(ctx, event)
    finally:
        dc_quota_runtime.get_quota_gate = original_get_gate

    assert handled is True
    assert captured["cancel"]["job_id"] == "agy-overflow"
    ctx.provider_manager.set_provider.assert_called_once()
    assert ctx.provider_manager.set_provider.call_args.kwargs["provider_id"] == (
        "aihubmix/gemini-3.5-flash"
    )
    assert event.message_str == "你好"
    event.set_result.assert_not_called()
    print("  ✅ queue card action: cancel agy queue → switch 池池 and replay prompt")


@pytest.mark.asyncio
async def test_antigravity_queue_card_action_keeps_queue_when_fallback_missing() -> (
    None
):
    from dc_router_adapter import maybe_handle_antigravity_queue_card_action

    ctx = _make_mock_ctx([])
    event = _make_mock_event(
        "__card_action__:"
        + json.dumps(
            {
                "value": {
                    "source": "antigravity_queue_card",
                    "action": "use_fallback",
                    "job_id": "agy-overflow",
                    "fallback_provider_id": "aihubmix/gemini-3.5-flash",
                    "original_prompt": "你好",
                }
            },
            ensure_ascii=False,
        ),
        "巅池-Agent小助手",
    )
    event.is_card_action = True

    handled = await maybe_handle_antigravity_queue_card_action(ctx, event)

    assert handled is True
    ctx.provider_manager.set_provider.assert_not_called()
    event.should_call_llm.assert_called_once_with(False)
    result_text = event.set_result.call_args.args[0].get_plain_text()
    assert "继续帮您保留排队位置" in result_text
    print("  ✅ queue card action: fallback missing → keep agy queue")


@pytest.mark.asyncio
async def test_e2e_dry_run_logs_but_does_not_switch() -> None:
    """dry_run=True: 跑判定 + log，但 *不* 调 set_provider。"""
    from dc_router_adapter import route_via_dc_router

    ctx = _make_mock_ctx(["aihubmix/gemini-3.5-flash", "codex/gpt-5.4"])
    event = _make_mock_event("帮我写一段端午客户问候话术", "巅池-Agent小助手")

    handled = await route_via_dc_router(ctx, event, dry_run=True)
    assert handled is False, "dry_run=True 必须返回 False 让 v1 兜底"
    ctx.provider_manager.set_provider.assert_not_called()
    print("  ✅ dry_run=True: 不调 set_provider，让 v1.0 实际处理")


@pytest.mark.asyncio
async def test_e2e_classifier_uses_gemini_31_pro_when_rules_uncertain() -> None:
    """规则不确定时: Gemini 3.1 Pro classifier 输出 JSON 意图。"""
    from astrbot.core.provider.entities import LLMResponse
    from dc_router_adapter import route_via_dc_router

    captured: dict = {}

    class FakeClassifierProvider:
        async def text_chat(self, *, prompt, system_prompt, contexts):
            captured["prompt"] = prompt
            captured["system_prompt"] = system_prompt
            captured["contexts"] = contexts
            return LLMResponse(
                role="assistant",
                completion_text=(
                    '{"intent":"ops_writing","confidence":0.91,'
                    '"reason":"user asks to organize external wording"}'
                ),
            )

    ctx = _make_mock_ctx(
        [
            "aihubmix/qwen3.6-flash",
            "aihubmix/gemini-3.5-flash",
            "aihubmix/gemini-3.1-pro-preview",
        ],
        provider_overrides={
            "aihubmix/gemini-3.1-pro-preview": FakeClassifierProvider(),
        },
    )
    event = _make_mock_event("帮我整理一下这段对外说法", "巅池-Agent小助手")

    handled = await route_via_dc_router(ctx, event, dry_run=False)

    assert handled is True
    actual_provider = ctx.provider_manager.set_provider.call_args.kwargs["provider_id"]
    assert actual_provider == "aihubmix/qwen3.6-flash"
    assert "JSON schema" in captured["system_prompt"]
    assert captured["contexts"] == []
    print("  ✅ classifier: 规则不确定 → Gemini 3.1 Pro JSON → ops_writing")


@pytest.mark.asyncio
async def test_e2e_multimodal_preprocess_then_reroute() -> None:
    """图片/截图: Gemini Flash 先转文本，再把摘要交回 router 继续判定。"""
    from astrbot.api.message_components import Image
    from astrbot.core.provider.entities import LLMResponse
    from dc_router_adapter import route_via_dc_router

    captured: dict = {}
    one_pixel_png = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
        "/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
    )

    class FakeCaptionProvider:
        async def text_chat(self, *, prompt, image_urls, audio_urls, contexts):
            captured["prompt"] = prompt
            captured["image_urls"] = image_urls
            captured["audio_urls"] = audio_urls
            captured["contexts"] = contexts
            return LLMResponse(
                role="assistant",
                completion_text="截图里有端午客户问候话术要求，重点是礼貌问候和合作感谢。",
            )

    ctx = _make_mock_ctx(
        ["aihubmix/gemini-3.5-flash", "aihubmix/deepseek-v4-pro"],
        provider_overrides={
            "aihubmix/gemini-3.5-flash": FakeCaptionProvider(),
        },
    )
    event = _make_mock_event("请根据这张图写一段客户问候话术", "巅池-Agent小助手")
    event.message_obj.message = [Image.fromBase64(one_pixel_png)]

    handled = await route_via_dc_router(ctx, event, dry_run=False)

    assert handled is True
    assert captured["image_urls"], "预处理应该把图片传给 Gemini Flash"
    assert captured["audio_urls"] == []
    assert "附件摘要" not in event.message_str
    assert "<attachment_summary>" in event.message_str
    assert "端午客户问候话术要求" in event.message_str
    assert all(not isinstance(comp, Image) for comp in event.message_obj.message)
    actual_provider = ctx.provider_manager.set_provider.call_args.kwargs["provider_id"]
    assert actual_provider == "aihubmix/deepseek-v4-pro"
    print("  ✅ multimodal: image → Gemini Flash caption/OCR → reroute → DeepSeek")


@pytest.mark.asyncio
async def test_e2e_ops_route_uses_codex_cli() -> None:
    """DevOps platform: ops route uses Codex CLI gpt-5.4 directly."""
    import cli_runner
    from cli_runner import CliResult
    from dc_router_adapter import route_via_dc_router

    captured: dict = {}

    class FakeCliRunner:
        def __init__(self, *args, **kwargs):
            captured["runner_init"] = {"args": args, "kwargs": kwargs}

        async def run_codex(self, prompt, *, model, timeout):
            captured["codex"] = {
                "prompt": prompt,
                "model": model,
                "timeout": timeout,
            }
            return CliResult(text="Hermes 状态摘要。", elapsed_sec=0.8)

    original_runner = cli_runner.CliRunner
    try:
        cli_runner.CliRunner = FakeCliRunner
        ctx = _make_mock_ctx([])
        event = _make_mock_event("Hermes 状态如何", "巅池-技术（DevOps）")
        handled = await route_via_dc_router(ctx, event, dry_run=False)
    finally:
        cli_runner.CliRunner = original_runner

    assert handled is True
    ctx.provider_manager.set_provider.assert_not_called()
    event.should_call_llm.assert_called_once_with(False)
    event.set_result.assert_called_once()
    assert captured["codex"]["model"] == "gpt-5.4"
    assert Path(captured["runner_init"]["kwargs"]["cwd"]) == Path(_DC_AGENT_ROOT)
    assert "Hermes 状态如何" in captured["codex"]["prompt"]
    print("  ✅ DevOps e2e: ops/system_status → cli/codex/gpt-5.4")


@pytest.mark.asyncio
async def test_e2e_provider_missing_falls_back() -> None:
    """provider 不在 available 列表时，应该 return False 让 plugin fallback v1.0。"""
    from dc_router_adapter import route_via_dc_router

    # 只有少数 provider 可用，故意把 ops_writing 的目标 (qwen3.6-flash) 排除
    ctx = _make_mock_ctx(["codex/gpt-5.4"])  # 缺 gemini flash
    event = _make_mock_event("帮我写一封邮件", "巅池-Agent小助手")
    handled = await route_via_dc_router(ctx, event, dry_run=False)
    assert handled is False, "target provider 不存在时应该 return False"
    ctx.provider_manager.set_provider.assert_not_called()
    print("  ✅ provider 缺失 → 不调 set_provider，return False 让 v1 兜底")


@pytest.mark.asyncio
async def test_media_route_detection() -> None:
    """生图 / 文生视频 / 静态图转视频 should be detected before normal LLM."""
    plugin_main = _load_llm_router_plugin_main()

    plugin = object.__new__(plugin_main.LLMRouterPlugin)

    image_event = _make_mock_event("生成一张竖版端午海报", "巅池-Agent小助手")
    image_route = await plugin._detect_media_route(
        image_event,
        image_event.message_str,
    )
    assert image_route is not None
    assert image_route.kind == "image"
    assert image_route.aspect_ratio == "portrait"

    video_event = _make_mock_event("生成视频：汽车在城市夜景中驶过", "巅池-Agent小助手")
    video_route = await plugin._detect_media_route(
        video_event,
        video_event.message_str,
    )
    assert video_route is not None
    assert video_route.kind == "text2video"

    cached_event = _make_mock_event("让这张图动起来，镜头推进", "巅池-Agent小助手")
    plugin_main._LAST_IMAGE_BY_SESSION[cached_event.unified_msg_origin] = (
        "/tmp/mock.png"
    )
    try:
        image2video_route = await plugin._detect_media_route(
            cached_event,
            cached_event.message_str,
        )
    finally:
        plugin_main._LAST_IMAGE_BY_SESSION.pop(cached_event.unified_msg_origin, None)
    assert image2video_route is not None
    assert image2video_route.kind == "image2video"
    assert image2video_route.image_path == "/tmp/mock.png"
    print("  ✅ media route: 生图 / 文生视频 / 静态图转视频 检测 OK")


@pytest.mark.asyncio
async def test_media_route_uses_raw_text_before_memory_injection(monkeypatch) -> None:
    """Memory context words like 图谱/素材 must not make a casual message become image generation."""
    import dc_memory_context

    plugin_main = _load_llm_router_plugin_main()
    import truth_intake_guard
    from unittest.mock import AsyncMock

    ctx = _make_mock_ctx(["aihubmix/qwen3.6-flash", "aihubmix/gemini-3.5-flash"])
    event = _make_mock_event("聊个1分钱的天", "巅池-Agent小助手")
    plugin = object.__new__(plugin_main.LLMRouterPlugin)
    plugin.context = ctx
    plugin._dc_queue_recovery_running = False
    plugin._classify_with_llm = AsyncMock(return_value=None)
    captured = {}

    async def fake_media_route(_event, text):
        captured["media_text"] = text
        return True

    def fake_inject(event, query_text=None):
        event.message_str = f"{event.message_str}\n\n<dc_agent_memory_context>知识图谱 素材 生成图片</dc_agent_memory_context>"
        return True

    monkeypatch.setattr(plugin, "_try_handle_media_route", fake_media_route)
    monkeypatch.setattr(
        dc_memory_context, "inject_memory_context_into_event", fake_inject
    )
    monkeypatch.setattr(
        truth_intake_guard,
        "maybe_handle_truth_intake",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        plugin_main,
        "_read_dc_router_config",
        lambda: {"enabled": True, "dry_run": True, "fallback_on_error": True},
    )

    await plugin.route(event)

    assert captured["media_text"] == "聊个1分钱的天"
    print("  ✅ media route: 使用原始消息，忽略记忆上下文触发词")


@pytest.mark.asyncio
async def test_dc_memory_retrieval_query_includes_recent_conversation(
    monkeypatch,
) -> None:
    """Short feedback memory retrieval should use recent context, not only 不满意."""
    import dc_memory_context
    import truth_intake_guard
    from unittest.mock import AsyncMock, MagicMock

    plugin_main_path = Path(_PLUGIN_DIR) / "main.py"
    spec = importlib.util.spec_from_file_location(
        "llm_router_main_for_memory_query_test",
        plugin_main_path,
    )
    assert spec is not None
    assert spec.loader is not None
    plugin_main = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = plugin_main
    spec.loader.exec_module(plugin_main)

    ctx = _make_mock_ctx(["aihubmix/qwen3.6-flash", "aihubmix/gemini-3.5-flash"])
    conversation = SimpleNamespace(
        history=json.dumps(
            [
                {"role": "user", "content": "帮我做五菱2026中秋传播方案"},
                {
                    "role": "assistant",
                    "content": "五菱2026中秋，要做陪你去看月亮的那台车。",
                },
            ],
            ensure_ascii=False,
        )
    )
    ctx.conversation_manager = MagicMock()
    ctx.conversation_manager.get_curr_conversation_id = AsyncMock(return_value="conv")
    ctx.conversation_manager.get_conversation = AsyncMock(return_value=conversation)

    event = _make_mock_event("不满意", "巅池-Agent小助手")
    plugin = object.__new__(plugin_main.LLMRouterPlugin)
    plugin.context = ctx
    plugin._dc_queue_recovery_running = False
    plugin._classify_with_llm = AsyncMock(return_value=None)
    captured = {}

    async def fake_media_route(_event, text):
        captured["media_text"] = text
        return True

    def fake_inject(event, query_text=None):
        captured["query_text"] = query_text
        return False

    monkeypatch.setattr(plugin, "_try_handle_media_route", fake_media_route)
    monkeypatch.setattr(
        dc_memory_context, "inject_memory_context_into_event", fake_inject
    )
    monkeypatch.setattr(
        truth_intake_guard,
        "maybe_handle_truth_intake",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        plugin_main,
        "_read_dc_router_config",
        lambda: {"enabled": True, "dry_run": True, "fallback_on_error": True},
    )

    await plugin.route(event)

    assert captured["media_text"] == "不满意"
    assert captured["query_text"] is not None
    assert "最近对话" in captured["query_text"]
    assert "五菱2026中秋" in captured["query_text"]
    assert "当前消息" in captured["query_text"]
    assert "不满意" in captured["query_text"]
    ctx.conversation_manager.get_curr_conversation_id.assert_awaited_once_with(
        event.unified_msg_origin
    )
    ctx.conversation_manager.get_conversation.assert_awaited_once_with(
        event.unified_msg_origin,
        "conv",
    )
    print("  ✅ dc-memory query: 最近对话 + 当前短反馈 一起作为检索依据")


@pytest.mark.asyncio
async def test_dc_memory_injection_does_not_pollute_intent_routing_text(
    monkeypatch,
) -> None:
    """Intent routing should classify the raw user turn, not injected memory text."""
    import dc_memory_context
    import dc_router_adapter
    import truth_intake_guard
    from unittest.mock import AsyncMock

    plugin_main_path = Path(_PLUGIN_DIR) / "main.py"
    spec = importlib.util.spec_from_file_location(
        "llm_router_main_for_intent_text_test",
        plugin_main_path,
    )
    assert spec is not None
    assert spec.loader is not None
    plugin_main = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = plugin_main
    spec.loader.exec_module(plugin_main)

    ctx = _make_mock_ctx(["aihubmix/qwen3.6-flash", "aihubmix/gemini-3.5-flash"])
    event = _make_mock_event("不满意", "巅池-Agent小助手")
    plugin = object.__new__(plugin_main.LLMRouterPlugin)
    plugin.context = ctx
    plugin._dc_queue_recovery_running = False
    captured = {}

    async def fake_classify(text):
        captured["classify_text"] = text
        return None

    async def fake_media_route(_event, text):
        captured["media_text"] = text
        return False

    def fake_inject(event, query_text=None):
        event.message_str = (
            f"{event.message_str}\n\n"
            "<dc_agent_memory_context>生成图片 深度分析 #高</dc_agent_memory_context>"
        )
        return True

    plugin._classify_with_llm = fake_classify
    monkeypatch.setattr(plugin, "_try_handle_media_route", fake_media_route)
    monkeypatch.setattr(
        dc_memory_context, "inject_memory_context_into_event", fake_inject
    )
    monkeypatch.setattr(
        truth_intake_guard,
        "maybe_handle_truth_intake",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        dc_router_adapter,
        "route_via_dc_router",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        plugin_main,
        "_read_dc_router_config",
        lambda: {"enabled": True, "dry_run": True, "fallback_on_error": True},
    )

    await plugin.route(event)

    assert event.message_str != "不满意"
    assert captured["media_text"] == "不满意"
    assert captured["classify_text"] == "不满意"
    ctx.provider_manager.set_provider.assert_not_awaited()
    print("  ✅ dc-memory: intent/provider routing uses raw short feedback text")


@pytest.mark.asyncio
async def test_dc_memory_injection_does_not_pollute_dc_router_decision_text(
    monkeypatch,
) -> None:
    """Formal dc-router should decide on raw text, then restore injected memory."""
    import dc_memory_context
    import dc_router_adapter
    import truth_intake_guard
    from unittest.mock import AsyncMock

    plugin_main_path = Path(_PLUGIN_DIR) / "main.py"
    spec = importlib.util.spec_from_file_location(
        "llm_router_main_for_dc_router_text_test",
        plugin_main_path,
    )
    assert spec is not None
    assert spec.loader is not None
    plugin_main = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = plugin_main
    spec.loader.exec_module(plugin_main)

    ctx = _make_mock_ctx(["aihubmix/gemini-3.5-flash"])
    event = _make_mock_event("不满意", "巅池-Agent小助手")
    plugin = object.__new__(plugin_main.LLMRouterPlugin)
    plugin.context = ctx
    plugin._dc_queue_recovery_running = False
    plugin._start_dc_queue_recovery = lambda: None
    plugin._classify_with_llm = AsyncMock(return_value=None)
    captured = {}

    async def fake_media_route(_event, text):
        captured["media_text"] = text
        return False

    def fake_inject(event, query_text=None):
        event.message_str = (
            f"{event.message_str}\n\n"
            "<dc_agent_memory_context>生成图片 深度分析 #高</dc_agent_memory_context>"
        )
        event.message_obj.message_str = event.message_str
        return True

    async def fake_route_via_dc_router(_context, event, dry_run):
        captured["dc_router_text"] = event.message_str
        captured["dc_router_message_obj_text"] = event.message_obj.message_str
        captured["dry_run"] = dry_run
        return False

    monkeypatch.setattr(plugin, "_try_handle_media_route", fake_media_route)
    monkeypatch.setattr(
        dc_memory_context, "inject_memory_context_into_event", fake_inject
    )
    monkeypatch.setattr(
        truth_intake_guard,
        "maybe_handle_truth_intake",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        dc_router_adapter,
        "route_via_dc_router",
        fake_route_via_dc_router,
    )
    monkeypatch.setattr(
        plugin_main,
        "_read_dc_router_config",
        lambda: {"enabled": True, "dry_run": False, "fallback_on_error": True},
    )

    await plugin.route(event)

    assert captured["media_text"] == "不满意"
    assert captured["dc_router_text"] == "不满意"
    assert captured["dc_router_message_obj_text"] == "不满意"
    assert captured["dry_run"] is False
    assert "<dc_agent_memory_context>" in event.message_str
    assert event.message_obj.message_str == event.message_str
    print("  ✅ dc-router: decision sees raw text, then memory context is restored")


def test_memory_context_does_not_retrieve_for_casual_chat() -> None:
    """Casual group chat should not receive NAS/Obsidian context."""
    import dc_memory_context

    assert dc_memory_context._should_retrieve("聊个1分钱的天") is False
    assert dc_memory_context._should_retrieve("帮我优化一下这一篇推文内容") is True
    assert dc_memory_context._should_retrieve("星光S这个方案是谁负责") is True
    print("  ✅ dc-memory: 闲聊不检索，业务资料问题才检索")


def test_memory_context_recalls_planning_fact_gate_memory(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Middle-platform planning fact-gate phrasing should recall approved memory."""
    import dc_memory_context
    from dc_engines.memory_governance.models import GovernedMemory
    from dc_engines.memory_governance.store import MemoryGovernanceStore

    governed_db = tmp_path / "governed_memory.db"
    store = MemoryGovernanceStore(governed_db)
    store.initialize()
    store.upsert_memory(
        GovernedMemory(
            memory_id="mem_planning_fact_gate",
            source_system="manual",
            source_id="obsidian_distillation:planning#proposal-fact-gate-v1",
            source_path="obsidian_distillation:planning/proposal-fact-gate-v1",
            source_hash="sha256:planning",
            title="中台-策划 Proposal Fact Gate Memory",
            summary="中台提案和客户可见方案需要先检查事实边界。",
            canonical_text="客户可见的中台提案需要检查事实边界，不得编造优惠、价格、权益或活动信息。",
            memory_kind="process",
            review_status="approved",
            confidence=0.9,
            sensitivity="internal",
            owner="中台-策划",
            project_id="planning_content_sop_workflow",
            tags=["中台-策划", "提案", "事实边界"],
            links=[],
            created_at="2026-06-06T00:00:00Z",
            updated_at="2026-06-06T00:00:00Z",
            approved_at="2026-06-06T00:00:00Z",
            approved_by="dianchi",
        )
    )
    monkeypatch.setattr(dc_memory_context, "GOVERNED_MEMORY_DB", governed_db)
    monkeypatch.setattr(dc_memory_context, "NAS_MEMORY_DB", tmp_path / "missing.db")

    query = "客户可见的中台提案需要检查哪些事实边界"

    assert dc_memory_context._should_retrieve(query) is True
    context = dc_memory_context.retrieve_memory_context(query)

    assert [item["memory_id"] for item in context["governed_memories"]] == [
        "mem_planning_fact_gate"
    ]
    print("  ✅ dc-memory: 中台提案事实边界自然语言可召回 approved memory")


def test_memory_context_recalls_expanded_planning_department_scenarios(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Expanded planning distillation keywords should recall approved memories."""
    import dc_memory_context
    from dc_engines.memory_governance.models import GovernedMemory
    from dc_engines.memory_governance.store import MemoryGovernanceStore

    governed_db = tmp_path / "governed_memory.db"
    store = MemoryGovernanceStore(governed_db)
    store.initialize()
    for memory_id, title, canonical_text, tags in [
        (
            "mem_planning_content_center",
            "中台-策划 User Content Center Ecosystem Memory",
            "内容中心和企微内容库要围绕用户运营、素材库、栏目和旅程节点规划。",
            ["内容中心", "用户运营", "企微内容库"],
        ),
        (
            "mem_planning_account_matrix",
            "中台-策划 Account Operation Column Matrix Memory",
            "账号运营要形成栏目矩阵，兼顾短视频、直播、活动新闻和用户故事。",
            ["账号运营", "栏目", "直播"],
        ),
        (
            "mem_planning_gift_material",
            "中台-策划 Gift And Material Creative Memory",
            "礼品物料创意要结合车型定位、用户场景、传播价值和制作注意事项。",
            ["礼品", "物料"],
        ),
    ]:
        store.upsert_memory(
            GovernedMemory(
                memory_id=memory_id,
                source_system="manual",
                source_id=f"obsidian_distillation:planning#{memory_id}",
                source_path=f"obsidian_distillation:planning/{memory_id}",
                source_hash=f"sha256:{memory_id}",
                title=title,
                summary=canonical_text,
                canonical_text=canonical_text,
                memory_kind="process",
                review_status="approved",
                confidence=0.9,
                sensitivity="internal",
                owner="中台-策划",
                project_id="planning_content_sop_workflow",
                tags=tags,
                links=[],
                created_at="2026-06-06T00:00:00Z",
                updated_at="2026-06-06T00:00:00Z",
                approved_at="2026-06-06T00:00:00Z",
                approved_by="dianchi",
            )
        )
    monkeypatch.setattr(dc_memory_context, "GOVERNED_MEMORY_DB", governed_db)
    monkeypatch.setattr(dc_memory_context, "NAS_MEMORY_DB", tmp_path / "missing.db")

    cases = {
        "中台内容中心和企微内容库怎么做用户运营": "mem_planning_content_center",
        "中台账号运营要怎么设计栏目和直播节奏": "mem_planning_account_matrix",
        "策划给缤果S做一套礼品物料创意": "mem_planning_gift_material",
    }
    for query, expected_memory_id in cases.items():
        context = dc_memory_context.retrieve_memory_context(query)
        memory_ids = [item["memory_id"] for item in context["governed_memories"]]
        assert memory_ids[0] == expected_memory_id
    print("  ✅ dc-memory: 中台内容中心/账号运营/礼品物料可召回 approved memory")


def test_memory_context_prefers_governed_approved_memory(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Approved governed memory should be injected before raw NAS fallback."""
    import dc_memory_context
    from dc_engines.memory_governance.models import GovernedMemory
    from dc_engines.memory_governance.store import MemoryGovernanceStore

    governed_db = tmp_path / "governed_memory.db"
    store = MemoryGovernanceStore(governed_db)
    store.initialize()
    store.upsert_memory(
        GovernedMemory(
            memory_id="mem_launch_approved",
            source_system="nas",
            source_id="nas:doc_1",
            source_path="/Users/dianchi/nas_kb/launch-sop.md",
            source_hash="sha256:approved",
            title="星光S launch SOP",
            summary="星光S launch SOP is owned by 谭媛尹.",
            canonical_text="星光S launch SOP is owned by 谭媛尹.",
            memory_kind="process",
            review_status="approved",
            confidence=0.93,
            sensitivity="internal",
            owner="谭媛尹",
            project_id="xingguang-s",
            tags=["星光S", "launch"],
            links=["[[星光S launch SOP]]"],
            created_at="2026-06-04T00:00:00Z",
            updated_at="2026-06-04T00:00:00Z",
            approved_at="2026-06-04T00:10:00Z",
            approved_by="dianchi",
        )
    )
    store.upsert_memory(
        GovernedMemory(
            memory_id="mem_launch_draft",
            source_system="nas",
            source_id="nas:doc_2",
            source_path="/Users/dianchi/nas_kb/launch-draft.md",
            source_hash="sha256:draft",
            title="星光S launch draft",
            summary="Draft should not be injected by default.",
            canonical_text="Draft should not be injected by default.",
            memory_kind="process",
            review_status="need_review",
            confidence=0.8,
            sensitivity="internal",
            owner="",
            project_id="xingguang-s",
            tags=["星光S"],
            links=[],
            created_at="2026-06-04T00:00:00Z",
            updated_at="2026-06-04T00:00:00Z",
        )
    )
    store.upsert_memory(
        GovernedMemory(
            memory_id="mem_launch_secret",
            source_system="nas",
            source_id="nas:doc_3",
            source_path="/Users/dianchi/nas_kb/launch-secret.md",
            source_hash="sha256:secret",
            title="星光S launch secret",
            summary="Secret should not be injected by default.",
            canonical_text="Secret should not be injected by default.",
            memory_kind="process",
            review_status="approved",
            confidence=0.8,
            sensitivity="secret",
            owner="",
            project_id="xingguang-s",
            tags=["星光S"],
            links=[],
            created_at="2026-06-04T00:00:00Z",
            updated_at="2026-06-04T00:00:00Z",
            approved_at="2026-06-04T00:10:00Z",
            approved_by="dianchi",
        )
    )
    monkeypatch.setattr(dc_memory_context, "GOVERNED_MEMORY_DB", governed_db)
    monkeypatch.setattr(dc_memory_context, "NAS_MEMORY_DB", tmp_path / "missing.db")

    context = dc_memory_context.retrieve_memory_context("星光S这个方案是谁负责")
    block = dc_memory_context.format_memory_context(context)

    assert [item["memory_id"] for item in context["governed_memories"]] == [
        "mem_launch_approved"
    ]
    assert "已通过 Obsidian 人工治理并批准" in block
    assert "mem_launch_approved" in block
    assert "mem_launch_draft" not in block
    assert "mem_launch_secret" not in block
    print("  ✅ dc-memory: 优先注入 approved governed memory，过滤未审核和 secret")


def test_chitchat_guard_normalizes_short_phrases() -> None:
    """Short chitchat variants should hit the local fast path."""
    plugin_main = _load_llm_router_plugin_main()

    assert plugin_main._normalize_chitchat_text("[At:ou_bot] 你好呀！") == "你好呀"
    assert plugin_main._chitchat_response_for("在吗？") is not None
    assert plugin_main._chitchat_response_for("帮我优化这篇推文") is None
    print("  ✅ chitchat guard: 短句归一化和业务长句放行 OK")


def test_chitchat_guard_private_short_circuits_llm() -> None:
    """Private short greetings should reply locally and stop LLM routing."""
    plugin_main = _load_llm_router_plugin_main()

    plugin_main._CHITCHAT_LAST_HIT.clear()
    plugin = object.__new__(plugin_main.LLMRouterPlugin)
    event = _make_mock_event("你好", "巅池-Agent小助手")

    handled = plugin._try_handle_chitchat_guard(event, event.message_str)

    assert handled is True
    event.should_call_llm.assert_called_once_with(False)
    event.set_result.assert_called_once()
    print("  ✅ chitchat guard: 私聊短句本地秒回")


def test_chitchat_guard_group_requires_at() -> None:
    """Group chitchat should only reply when the assistant was addressed."""
    plugin_main = _load_llm_router_plugin_main()

    plugin_main._CHITCHAT_LAST_HIT.clear()
    plugin = object.__new__(plugin_main.LLMRouterPlugin)
    event = _make_mock_event("你好", "巅池-Agent小助手")
    event.unified_msg_origin = "巅池-Agent小助手:GroupMessage:oc_group"
    event.get_group_id.return_value = "oc_group"
    event.is_at_or_wake_command = False

    assert plugin._try_handle_chitchat_guard(event, event.message_str) is False
    event.set_result.assert_not_called()

    event.is_at_or_wake_command = True
    assert plugin._try_handle_chitchat_guard(event, "[At:ou_bot] 你好") is True
    event.set_result.assert_called_once()
    print("  ✅ chitchat guard: 群聊必须 @ 才秒回")


def test_chitchat_guard_records_safe_misses(monkeypatch, tmp_path: Path) -> None:
    """Unknown short casual phrases are logged as candidates; business-like phrases are ignored."""
    plugin_main = _load_llm_router_plugin_main()

    miss_path = tmp_path / "misses.jsonl"
    monkeypatch.setattr(plugin_main, "_CHITCHAT_MISS_LOG_PATH", miss_path)
    plugin = object.__new__(plugin_main.LLMRouterPlugin)
    event = _make_mock_event("滴滴", "巅池-Agent小助手")

    assert plugin._try_handle_chitchat_guard(event, "滴滴") is False
    assert miss_path.exists()
    assert '"normalized_text": "滴滴"' in miss_path.read_text(encoding="utf-8")

    event = _make_mock_event("查报表", "巅池-Agent小助手")
    assert plugin._try_handle_chitchat_guard(event, "查报表") is False
    assert "查报表" not in miss_path.read_text(encoding="utf-8")
    print("  ✅ chitchat guard: 安全候选沉淀，短业务词不学习")


def test_chitchat_guard_reads_hot_keyword_overrides(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Applied distillation keywords should hit the local fast path without code edits."""
    plugin_main = _load_llm_router_plugin_main()

    overrides_path = tmp_path / "assistant_language_overrides.json"
    overrides_path.write_text(
        json.dumps(
            {
                "chitchat": {
                    "keywords": {"greeting": ["滴滴"]},
                    "responses": {"greeting": ["您好，我在的。您直接发需求就好。"]},
                },
                "intent_aliases": [],
                "tone_templates": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        plugin_main,
        "_ASSISTANT_LANGUAGE_OVERRIDES_PATH",
        overrides_path,
    )

    assert plugin_main._chitchat_response_for("滴滴") == (
        "您好，我在的。您直接发需求就好。"
    )
    print("  ✅ chitchat guard: 已审批短句热配置生效")


def test_keyword_fallback_reads_hot_intent_aliases(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Applied intent aliases should participate in keyword fallback routing."""
    plugin_main = _load_llm_router_plugin_main()

    overrides_path = tmp_path / "assistant_language_overrides.json"
    overrides_path.write_text(
        json.dumps(
            {
                "chitchat": {"keywords": {}, "responses": {}},
                "intent_aliases": [
                    {
                        "pattern": "短一点|精简",
                        "intent": "writing",
                        "source": "test",
                    }
                ],
                "tone_templates": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        plugin_main,
        "_ASSISTANT_LANGUAGE_OVERRIDES_PATH",
        overrides_path,
    )
    plugin = object.__new__(plugin_main.LLMRouterPlugin)

    assert plugin._match_keywords("这段太长了，帮我短一点") == "writing"
    print("  ✅ keyword fallback: 已审批意图别名热配置生效")


def test_business_tone_templates_are_injected_from_hot_overrides(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Applied tone templates should guide business writing requests at runtime."""
    plugin_main = _load_llm_router_plugin_main()

    overrides_path = tmp_path / "assistant_language_overrides.json"
    overrides_path.write_text(
        json.dumps(
            {
                "chitchat": {"keywords": {}, "responses": {}},
                "intent_aliases": [],
                "tone_templates": [
                    {
                        "name": "deepseek_archive_content_director_workflow",
                        "body": "减少纯口播，强调轻量化拍摄。",
                        "source": "test",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        plugin_main,
        "_ASSISTANT_LANGUAGE_OVERRIDES_PATH",
        overrides_path,
    )
    event = _make_mock_event("帮我写一条五菱缤果S视频脚本", "巅池-Agent小助手")

    assert plugin_main._inject_assistant_tone_context_into_event(
        event,
        "帮我写一条五菱缤果S视频脚本",
    )
    assert "<assistant_tone_context>" in event.message_str
    assert "减少纯口播，强调轻量化拍摄。" in event.message_str
    assert event.message_obj.message_str == event.message_str
    print("  ✅ tone templates: 业务脚本请求热配置注入生效")


def test_business_tone_templates_do_not_inject_for_chitchat(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Tone templates should not pollute casual short chat."""
    plugin_main = _load_llm_router_plugin_main()

    overrides_path = tmp_path / "assistant_language_overrides.json"
    overrides_path.write_text(
        json.dumps(
            {
                "chitchat": {"keywords": {}, "responses": {}},
                "intent_aliases": [],
                "tone_templates": [
                    {
                        "name": "deepseek_archive_content_director_workflow",
                        "body": "减少纯口播，强调轻量化拍摄。",
                        "source": "test",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        plugin_main,
        "_ASSISTANT_LANGUAGE_OVERRIDES_PATH",
        overrides_path,
    )
    event = _make_mock_event("你好", "巅池-Agent小助手")

    assert plugin_main._inject_assistant_tone_context_into_event(event, "你好") is False
    assert "<assistant_tone_context>" not in event.message_str
    print("  ✅ tone templates: 闲聊不注入")


def test_department_profiles_inject_planning_and_client_guidance() -> None:
    """Planning and client-side requests should receive department workflow hints."""
    plugin_main = _load_llm_router_plugin_main()

    planning_event = _make_mock_event(
        "中台策划帮我做一版活动传播方案和视频分镜",
        "巅池-Agent小助手",
    )
    client_event = _make_mock_event(
        "客户那边要一套老客户邀约话术和私域文案",
        "巅池-Agent小助手",
    )

    assert plugin_main._inject_assistant_tone_context_into_event(
        planning_event,
        planning_event.message_str,
    )
    assert "中台-策划" in planning_event.message_str
    assert "创意 brief" in planning_event.message_str
    assert plugin_main._inject_assistant_tone_context_into_event(
        client_event,
        client_event.message_str,
    )
    assert "客户部" in client_event.message_str
    assert "不得编造优惠" in client_event.message_str
    print("  ✅ department profiles: 策划与客户侧自然语言触发部门提示")


def test_department_memory_keyword_prompts_before_injection(monkeypatch) -> None:
    """Natural department keywords should prompt before approved memory injection."""
    plugin_main = _load_llm_router_plugin_main()

    plugin_main._PENDING_DEPARTMENT_MEMORY_PROMPTS.clear()
    plugin = object.__new__(plugin_main.LLMRouterPlugin)
    event = _make_mock_event("中台账号运营要怎么设计栏目和直播节奏", "巅池-Agent小助手")

    monkeypatch.setattr(
        plugin_main,
        "_has_approved_department_memory_for_prompt",
        lambda text, profiles: True,
    )

    decision = plugin._try_handle_department_memory_activation_prompt(
        event,
        raw_text=event.message_str,
        query_text=event.message_str,
    )

    assert decision.stop is True
    assert decision.inject_memory is False
    event.should_call_llm.assert_called_once_with(False)
    event.set_result.assert_called_once()
    result = event.set_result.call_args.args[0]
    assert "是否调用已通过 Obsidian 审核的部门记忆" in str(result.chain)
    assert "<dc_agent_memory_context>" not in event.message_str
    print("  ✅ department memory prompt: 自然关键词先提示，不偷注入")


def test_department_memory_confirmation_injects_original_request(monkeypatch) -> None:
    """A confirmation turn should resume the original request with memory enabled."""
    plugin_main = _load_llm_router_plugin_main()

    plugin_main._PENDING_DEPARTMENT_MEMORY_PROMPTS.clear()
    plugin = object.__new__(plugin_main.LLMRouterPlugin)
    first_event = _make_mock_event(
        "中台账号运营要怎么设计栏目和直播节奏", "巅池-Agent小助手"
    )
    confirm_event = _make_mock_event("调用记忆", "巅池-Agent小助手")
    confirm_event.unified_msg_origin = first_event.unified_msg_origin

    monkeypatch.setattr(
        plugin_main,
        "_has_approved_department_memory_for_prompt",
        lambda text, profiles: True,
    )

    first_decision = plugin._try_handle_department_memory_activation_prompt(
        first_event,
        raw_text=first_event.message_str,
        query_text=first_event.message_str,
    )
    assert first_decision.stop is True

    confirm_decision = plugin._try_handle_department_memory_activation_prompt(
        confirm_event,
        raw_text=confirm_event.message_str,
        query_text=confirm_event.message_str,
    )

    assert confirm_decision.stop is False
    assert confirm_decision.inject_memory is True
    assert confirm_decision.effective_text == "中台账号运营要怎么设计栏目和直播节奏"
    assert confirm_decision.memory_query_text == "中台账号运营要怎么设计栏目和直播节奏"
    assert confirm_event.message_str == "中台账号运营要怎么设计栏目和直播节奏"
    assert (
        first_event.unified_msg_origin
        not in plugin_main._PENDING_DEPARTMENT_MEMORY_PROMPTS
    )
    print("  ✅ department memory prompt: 确认后恢复原请求并允许注入")


def test_department_memory_card_action_confirms_pending_prompt(monkeypatch) -> None:
    """Trusted card actions should confirm a pending department memory suggestion."""
    plugin_main = _load_llm_router_plugin_main()

    plugin_main._PENDING_DEPARTMENT_MEMORY_PROMPTS.clear()
    plugin = object.__new__(plugin_main.LLMRouterPlugin)
    first_event = _make_mock_event(
        "中台账号运营要怎么设计栏目和直播节奏", "巅池-Agent小助手"
    )

    monkeypatch.setattr(
        plugin_main,
        "_has_approved_department_memory_for_prompt",
        lambda text, profiles: True,
    )

    first_decision = plugin._try_handle_department_memory_activation_prompt(
        first_event,
        raw_text=first_event.message_str,
        query_text=first_event.message_str,
    )
    assert first_decision.stop is True

    action_event = _make_mock_event(
        "__card_action__:"
        + json.dumps(
            {
                "value": {
                    "source": "department_memory_prompt",
                    "action": "confirm",
                    "suggestion_id": first_decision.suggestion_id,
                }
            },
            ensure_ascii=False,
        ),
        "巅池-Agent小助手",
    )
    action_event.unified_msg_origin = first_event.unified_msg_origin
    action_event.is_card_action = True

    confirm_decision = plugin._try_handle_department_memory_activation_prompt(
        action_event,
        raw_text=action_event.message_str,
        query_text=action_event.message_str,
    )

    assert confirm_decision.inject_memory is True
    assert confirm_decision.effective_text == "中台账号运营要怎么设计栏目和直播节奏"
    assert action_event.message_str == "中台账号运营要怎么设计栏目和直播节奏"
    print("  ✅ department memory prompt: 可信卡片 action 可确认 pending suggestion")


def test_department_memory_card_action_dismisses_pending_prompt(monkeypatch) -> None:
    """Trusted dismiss actions should resume the original request without memory."""
    plugin_main = _load_llm_router_plugin_main()

    plugin_main._PENDING_DEPARTMENT_MEMORY_PROMPTS.clear()
    plugin = object.__new__(plugin_main.LLMRouterPlugin)
    first_event = _make_mock_event(
        "中台账号运营要怎么设计栏目和直播节奏", "巅池-Agent小助手"
    )

    monkeypatch.setattr(
        plugin_main,
        "_has_approved_department_memory_for_prompt",
        lambda text, profiles: True,
    )

    first_decision = plugin._try_handle_department_memory_activation_prompt(
        first_event,
        raw_text=first_event.message_str,
        query_text=first_event.message_str,
    )
    assert first_decision.stop is True

    action_event = _make_mock_event(
        "__card_action__:"
        + json.dumps(
            {
                "value": {
                    "source": "department_memory_prompt",
                    "action": "dismiss",
                    "suggestion_id": first_decision.suggestion_id,
                }
            },
            ensure_ascii=False,
        ),
        "巅池-Agent小助手",
    )
    action_event.unified_msg_origin = first_event.unified_msg_origin
    action_event.is_card_action = True

    dismiss_decision = plugin._try_handle_department_memory_activation_prompt(
        action_event,
        raw_text=action_event.message_str,
        query_text=action_event.message_str,
    )

    assert dismiss_decision.dismissed is True
    assert dismiss_decision.inject_memory is False
    assert dismiss_decision.effective_text == "中台账号运营要怎么设计栏目和直播节奏"
    assert action_event.message_str == "中台账号运营要怎么设计栏目和直播节奏"
    assert (
        first_event.unified_msg_origin
        not in plugin_main._PENDING_DEPARTMENT_MEMORY_PROMPTS
    )
    print("  ✅ department memory prompt: 可信卡片 dismiss 恢复原请求但不注入")


def test_department_memory_card_action_rejects_untrusted_payload(monkeypatch) -> None:
    """Forged department memory card action payloads should stop without applying."""
    plugin_main = _load_llm_router_plugin_main()

    plugin_main._PENDING_DEPARTMENT_MEMORY_PROMPTS.clear()
    plugin = object.__new__(plugin_main.LLMRouterPlugin)
    first_event = _make_mock_event(
        "中台账号运营要怎么设计栏目和直播节奏", "巅池-Agent小助手"
    )

    monkeypatch.setattr(
        plugin_main,
        "_has_approved_department_memory_for_prompt",
        lambda text, profiles: True,
    )
    first_decision = plugin._try_handle_department_memory_activation_prompt(
        first_event,
        raw_text=first_event.message_str,
        query_text=first_event.message_str,
    )

    forged_event = _make_mock_event(
        "__card_action__:"
        + json.dumps(
            {
                "value": {
                    "source": "department_memory_prompt",
                    "action": "confirm",
                    "suggestion_id": first_decision.suggestion_id,
                }
            },
            ensure_ascii=False,
        ),
        "巅池-Agent小助手",
    )
    forged_event.unified_msg_origin = first_event.unified_msg_origin

    decision = plugin._try_handle_department_memory_activation_prompt(
        forged_event,
        raw_text=forged_event.message_str,
        query_text=forged_event.message_str,
    )

    assert decision.stop is True
    assert decision.inject_memory is False
    forged_event.should_call_llm.assert_called_once_with(False)
    forged_event.set_result.assert_called_once()
    print("  ✅ department memory prompt: 非可信卡片 action 不会应用记忆")


@pytest.mark.asyncio
async def test_department_memory_prompt_sends_confirm_card_when_lark() -> None:
    """Lark prompt path should send a card with confirm/dismiss action payloads."""
    plugin_main = _load_llm_router_plugin_main()

    captured: dict = {}

    class FakeStreamer:
        async def start(self, *, chat_id, receive_id_type, card):
            captured["chat_id"] = chat_id
            captured["receive_id_type"] = receive_id_type
            captured["card"] = card
            return SimpleNamespace(message_id="om_department_memory_prompt")

    ctx = _make_mock_ctx(["aihubmix/qwen3.6-flash"])
    ctx.feishu_streamers = {"巅池-Agent小助手": FakeStreamer()}
    plugin = object.__new__(plugin_main.LLMRouterPlugin)
    plugin.context = ctx
    event = _make_mock_event("中台账号运营要怎么设计栏目", "巅池-Agent小助手")
    event.get_platform_name.return_value = "lark"
    event.message_obj.raw_message = SimpleNamespace(chat_id="")
    state = plugin_main._DepartmentMemoryPromptState(
        suggestion_id="dmpp_test_card",
        conversation_id="lark:巅池-Agent小助手:test-session:test-user",
        original_text=event.message_str,
        query_text=event.message_str,
        department_ids=("planning",),
        department_names=("中台-策划",),
        profile_ids=("planning_content_sop_workflow",),
        created_at=0.0,
    )

    await plugin._send_department_memory_prompt_response(event, state)

    assert captured["chat_id"] == "test-user"
    values = [
        action["value"]
        for element in captured["card"]["elements"]
        if element.get("tag") == "action"
        for action in element["actions"]
    ]
    assert {
        "source": "department_memory_prompt",
        "action": "confirm",
        "suggestion_id": "dmpp_test_card",
    } in values
    assert {
        "source": "department_memory_prompt",
        "action": "dismiss",
        "suggestion_id": "dmpp_test_card",
    } in values
    event.should_call_llm.assert_called_once_with(False)
    event.set_result.assert_called_once()
    print("  ✅ department memory prompt: Lark 发卡包含可信 confirm/dismiss payload")


def test_explicit_memory_lookup_skips_department_prompt(monkeypatch) -> None:
    """Explicit memory lookup phrasing should keep the existing direct recall path."""
    plugin_main = _load_llm_router_plugin_main()

    plugin_main._PENDING_DEPARTMENT_MEMORY_PROMPTS.clear()
    plugin = object.__new__(plugin_main.LLMRouterPlugin)
    event = _make_mock_event(
        "查一下之前中台账号运营栏目矩阵的记忆",
        "巅池-Agent小助手",
    )

    monkeypatch.setattr(
        plugin_main,
        "_has_approved_department_memory_for_prompt",
        lambda text, profiles: True,
    )

    decision = plugin._try_handle_department_memory_activation_prompt(
        event,
        raw_text=event.message_str,
        query_text=event.message_str,
    )

    assert decision.stop is False
    assert decision.inject_memory is True
    event.set_result.assert_not_called()
    print("  ✅ department memory prompt: 明确查记忆时跳过提示，走直接召回")


@pytest.mark.asyncio
async def test_truth_intake_guard_respects_enabled_off() -> None:
    """enabled=false should make truth intake a no-op and keep v1.0 routing alive."""
    plugin_main = _load_llm_router_plugin_main()
    import truth_intake_guard
    from unittest.mock import AsyncMock

    class FailEngine:
        async def create_task(self, request):
            raise AssertionError("enabled=false must not create intake tasks")

    ctx = _make_mock_ctx(["aihubmix/qwen3.6-flash", "aihubmix/gemini-3.5-flash"])
    ctx.harness_engine = FailEngine()
    event = _make_mock_event("帮我写一份公司员工公告", "巅池-Agent小助手")

    handled = await truth_intake_guard.maybe_handle_truth_intake(
        ctx,
        event,
        {"enabled": False, "dry_run": True, "fallback_on_error": True},
    )

    assert handled is False
    event.should_call_llm.assert_not_called()
    event.set_result.assert_not_called()

    plugin = object.__new__(plugin_main.LLMRouterPlugin)
    plugin.context = ctx
    plugin._dc_queue_recovery_running = False
    plugin._classify_with_llm = AsyncMock(return_value=None)
    plugin._try_handle_media_route = AsyncMock(return_value=False)

    original_read_config = plugin_main._read_dc_router_config
    try:
        plugin_main._read_dc_router_config = lambda: {
            "enabled": False,
            "dry_run": True,
            "fallback_on_error": True,
        }
        await plugin.route(event)
    finally:
        plugin_main._read_dc_router_config = original_read_config

    ctx.provider_manager.set_provider.assert_called_once()
    actual_provider = ctx.provider_manager.set_provider.call_args.kwargs["provider_id"]
    assert actual_provider == "aihubmix/qwen3.6-flash"
    event.should_call_llm.assert_not_called()
    event.set_result.assert_not_called()
    print("  ✅ truth intake: enabled=false 不拦截，v1.0 继续路由")


@pytest.mark.asyncio
async def test_truth_intake_guard_dry_run_writes_task_without_intercept() -> None:
    """dry_run=true should judge/write the intake task but not stop the event."""
    import truth_intake_guard

    captured: dict = {}

    class FakeEngine:
        async def create_task(self, request):
            captured["request"] = request
            return SimpleNamespace(
                task_id="truth-task-dryrun",
                domain=request.domain,
                status="pending",
                payload=request.payload,
            )

        async def set_status(self, task_id, status, *, result=None, event_payload=None):
            captured["status"] = {
                "task_id": task_id,
                "status": status,
                "event_payload": event_payload,
            }
            return SimpleNamespace(
                task_id=task_id,
                domain=truth_intake_guard.INTAKE_DOMAIN,
                status=status,
                payload=captured["request"].payload,
            )

    ctx = _make_mock_ctx([])
    ctx.harness_engine = FakeEngine()
    event = _make_mock_event("帮我写一份公司员工公告", "巅池-Agent小助手")

    handled = await truth_intake_guard.maybe_handle_truth_intake(
        ctx,
        event,
        {"enabled": True, "dry_run": True, "fallback_on_error": True},
    )

    assert handled is False
    assert captured["request"].domain == truth_intake_guard.INTAKE_DOMAIN
    assert captured["status"]["status"] == "blocked"
    event.should_call_llm.assert_not_called()
    event.set_result.assert_not_called()
    print("  ✅ truth intake: dry_run 写 task 但不拦截")


@pytest.mark.asyncio
async def test_truth_intake_guard_engine_unavailable_does_not_intercept() -> None:
    """If Harness is unavailable, truth intake must fall back to v1.0."""
    import truth_intake_guard

    ctx = _make_mock_ctx([])
    ctx.harness_engine = None
    event = _make_mock_event("帮我写一份公司员工公告", "巅池-Agent小助手")

    handled = await truth_intake_guard.maybe_handle_truth_intake(
        ctx,
        event,
        {"enabled": True, "dry_run": False, "fallback_on_error": True},
    )

    assert handled is False
    event.should_call_llm.assert_not_called()
    event.set_result.assert_not_called()
    print("  ✅ truth intake: Harness 不可用时不拦截")


@pytest.mark.asyncio
async def test_truth_intake_blocks_company_fact_without_sources() -> None:
    """Company factual work should stop and ask for verifiable materials first."""
    import truth_intake_guard

    captured: dict = {}

    class FakeEngine:
        async def create_task(self, request):
            captured["request"] = request
            return SimpleNamespace(
                task_id="truth-task-001",
                domain=request.domain,
                status="pending",
                payload=request.payload,
            )

        async def set_status(self, task_id, status, *, result=None, event_payload=None):
            captured["status"] = {
                "task_id": task_id,
                "status": status,
                "event_payload": event_payload,
            }
            return SimpleNamespace(
                task_id=task_id,
                domain=truth_intake_guard.INTAKE_DOMAIN,
                status=status,
                payload=captured["request"].payload,
            )

    ctx = _make_mock_ctx([])
    ctx.harness_engine = FakeEngine()
    event = _make_mock_event("帮我写一份公司员工公告", "巅池-Agent小助手")

    handled = await truth_intake_guard.maybe_handle_truth_intake(ctx, event)

    assert handled is True
    assert captured["request"].domain == truth_intake_guard.INTAKE_DOMAIN
    assert captured["status"]["status"] == "blocked"
    event.should_call_llm.assert_called_once_with(False)
    event.set_result.assert_called_once()
    result_text = event.set_result.call_args.args[0].get_plain_text()
    assert "我先帮你把这件事稳住" in result_text
    assert "为了不让内容失真" in result_text
    assert "真实背景" in result_text
    print("  ✅ truth intake: 公司事实缺资料 → blocked task + 要材料")


@pytest.mark.asyncio
async def test_truth_intake_resumes_blocked_task_with_materials() -> None:
    """Supplementary materials should archive, unblock the task, and continue routing."""
    import truth_intake_guard

    captured: dict = {}

    class FakeStore:
        async def list_tasks_for_session(self, session_id, *, limit=10, statuses=None):
            return [
                SimpleNamespace(
                    task_id="truth-blocked-001",
                    domain=truth_intake_guard.INTAKE_DOMAIN,
                    status="blocked",
                    payload={
                        "source": truth_intake_guard.INTAKE_SOURCE,
                        "original_text": "帮我写一份公司员工公告",
                    },
                )
            ]

    class FakeEngine:
        async def merge_payload(self, task_id, patch, *, event_type="payload_merged"):
            captured["payload_patch"] = {
                "task_id": task_id,
                "patch": patch,
                "event_type": event_type,
            }

        async def append_trace(self, task_id, event_type, payload):
            captured["trace"] = {
                "task_id": task_id,
                "event_type": event_type,
                "payload": payload,
            }

        async def mark_in_progress(self, task_id, *, note=None):
            captured["in_progress"] = {"task_id": task_id, "note": note}
            return SimpleNamespace(task_id=task_id, status="in_progress")

    original_root = truth_intake_guard.INTAKE_ROOT
    with tempfile.TemporaryDirectory() as tmpdir:
        truth_intake_guard.INTAKE_ROOT = Path(tmpdir)
        try:
            ctx = _make_mock_ctx([])
            ctx.harness_store = FakeStore()
            ctx.harness_engine = FakeEngine()
            event = _make_mock_event(
                "补充资料如下：本周五 18:00 做全员系统维护通知，影响范围是内部后台。",
                "巅池-Agent小助手",
            )
            handled = await truth_intake_guard.maybe_handle_truth_intake(ctx, event)
        finally:
            truth_intake_guard.INTAKE_ROOT = original_root

    assert handled is False
    assert captured["payload_patch"]["event_type"] == (
        "truth_materials_payload_attached"
    )
    assert "archive_dir" in captured["payload_patch"]["patch"]
    assert "attachments" in captured["payload_patch"]["patch"]
    assert captured["trace"]["event_type"] == "truth_materials_received"
    assert captured["in_progress"]["task_id"] == "truth-blocked-001"
    assert "帮我写一份公司员工公告" in event.message_str
    assert "<dc_truth_source" in event.message_str
    assert "不得编造公司事实" in event.message_str
    event.set_result.assert_not_called()
    print("  ✅ truth intake: 补充资料 → 归档 + task in_progress + router 继续")


@pytest.mark.asyncio
async def test_harness_state_injector_adds_truth_guard_without_active_tasks() -> None:
    """The truth guard should apply even when there are no active Harness tasks."""
    module_path = (
        Path(_DC_AGENT_ROOT) / "data" / "plugins" / "harness_state_injector" / "main.py"
    )
    spec = importlib.util.spec_from_file_location(
        "dc_harness_state_injector_test",
        module_path,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    plugin = object.__new__(module.HarnessStateInjectorPlugin)
    plugin.context = SimpleNamespace(harness_store=None)
    event = _make_mock_event("你好，今天聊两句", "巅池-Agent小助手")
    req = SimpleNamespace(system_prompt="BASE")

    await plugin.inject_active_tasks(event, req)

    assert "BASE" in req.system_prompt
    assert "DC-Agent 真实性铁律" in req.system_prompt
    assert "不能编造事实" in req.system_prompt
    assert "温柔、耐心、体贴" in req.system_prompt
    print("  ✅ harness injector: 无 active task 时也注入真实性铁律")


@pytest.mark.asyncio
async def test_e2e_deep_routes_to_claude_opus() -> None:
    """#深度 should switch directly to AIHubMix Claude Opus 4.8."""
    from dc_router_adapter import route_via_dc_router

    ctx = _make_mock_ctx(["aihubmix/claude-opus-4-8"])
    event = _make_mock_event("#深度 帮我做一份品牌战略报告", "巅池-Agent小助手")

    handled = await route_via_dc_router(ctx, event, dry_run=False)

    assert handled is True
    ctx.provider_manager.set_provider.assert_called_once()
    actual_provider = ctx.provider_manager.set_provider.call_args.kwargs["provider_id"]
    assert actual_provider == "aihubmix/claude-opus-4-8"
    event.set_result.assert_not_called()
    ctx.send_message.assert_not_called()
    print("  ✅ deep route: #深度 → AIHubMix Claude Opus 4.8")


@pytest.mark.asyncio
async def test_e2e_creative_insight_aihubmix_direct() -> None:
    """#创意/#洞察: direct provider switch to the configured specialist models."""
    from dc_router_adapter import route_via_dc_router

    cases = [
        ("#创意 帮我写五菱端午营销 slogan", "aihubmix/deepseek-v4-pro"),
        ("#洞察 分析五菱新能源用户画像", "aihubmix/claude-sonnet-4-6"),
    ]
    for text, expected_provider in cases:
        ctx = _make_mock_ctx([expected_provider])
        event = _make_mock_event(text, "巅池-Agent小助手")
        handled = await route_via_dc_router(ctx, event, dry_run=False)

        assert handled is True
        ctx.provider_manager.set_provider.assert_called_once()
        actual_provider = ctx.provider_manager.set_provider.call_args.kwargs[
            "provider_id"
        ]
        assert actual_provider == expected_provider
        event.set_result.assert_not_called()
        ctx.send_message.assert_not_called()
    print("  ✅ creative/insight: #创意/#洞察 → specialist providers")


@pytest.mark.asyncio
async def test_queue_recovery_no_pending_jobs() -> None:
    """Queue recovery should be a no-op when there are no pending jobs."""
    import dc_router_adapter

    class FakeGate:
        async def list_pending_jobs(self, *, limit=20):
            return []

    ctx = _make_mock_ctx([])
    resumed = await dc_router_adapter._resume_pending_harness_cli_jobs(ctx, FakeGate())

    assert resumed == 0
    ctx.send_message.assert_not_called()
    print("  ✅ queue recovery: no pending jobs → no-op")


@pytest.mark.asyncio
async def test_queue_recovery_skips_pending_gemini_cli_job() -> None:
    """Persisted Gemini CLI jobs are no longer resumed by the dc_router."""
    import dc_router_adapter
    from harness import QueueJob, QueueStatus

    payload = {
        "provider_id": "cli/gemini-3.1-pro-preview",
        "backend": "gemini",
        "model": "gemini-3.1-pro-preview",
        "effort": None,
        "umo": "lark:巅池-Agent小助手:test-session",
        "prompt": "创意任务正文",
        "task_label": "创意任务",
    }
    captured: dict = {}

    class FakeGate:
        async def list_pending_jobs(self, *, limit=20):
            return [
                QueueJob(
                    job_id="persisted-front-job",
                    primary_resource_key="gemini_cli_pro",
                    resource_keys=("gemini_cli_pro",),
                    status=QueueStatus.PENDING,
                    payload=payload,
                    session_id=payload["umo"],
                )
            ]

        async def start_pending_job(self, job_id):
            captured["start_job_id"] = job_id
            raise AssertionError("Gemini CLI jobs should not be restarted")

    original_create_task = dc_router_adapter.asyncio.create_task
    created_tasks = []

    def tracking_create_task(coro):
        task = original_create_task(coro)
        created_tasks.append(task)
        return task

    try:
        dc_router_adapter.asyncio.create_task = tracking_create_task
        ctx = _make_mock_ctx([])
        resumed = await dc_router_adapter._resume_pending_harness_cli_jobs(
            ctx,
            FakeGate(),
        )
    finally:
        dc_router_adapter.asyncio.create_task = original_create_task

    assert resumed == 0
    assert "start_job_id" not in captured
    assert created_tasks == []
    ctx.send_message.assert_not_called()
    print("  ✅ queue recovery: pending Gemini CLI job is skipped")


# ─────────────────── 主入口 ───────────────────


async def main() -> None:
    print("=" * 60)
    print("dc-router 路径单元测试 (步骤 5)")
    print("=" * 60)

    print("\n[1. 配置读取]")
    test_config_current_state_active()
    test_dc_router_platform_allowlist()
    test_config_missing_file_safe_default()
    test_config_broken_json_safe_default()
    test_default_cmd_config_contains_dc_router_providers()

    print("\n[2. DCRouter business 路由决策]")
    await test_router_basic_intents()
    await test_router_deep_creative_uses_claude_opus_47()
    await test_router_deep_insight_uses_claude_opus_48()
    test_deep_creative_and_insight_use_aihubmix_claude_without_queue()

    print("\n[2b. DCRouter ops 路由代码]")
    await test_router_ops_intents()

    print("\n[2c. DevOps platform 默认走 ops]")
    await test_devops_platform_uses_ops_router_by_default()

    print("\n[2d. business / ops 隔离测试]")
    await test_router_business_unaffected_by_ops_keywords()

    print("\n[3. QuotaGate 端到端]")
    await test_quota_gate_lifecycle()
    await test_quota_gate_start_pending_job()
    await test_quota_gate_cancel_pending_job()

    print("\n[4. Adapter 边界层]")
    await test_adapter_route_signature()
    test_parse_cli_provider_sonnet_high()
    test_parse_cli_provider_codex()
    test_parse_cli_provider_antigravity()

    print("\n[5. CLI runner]")
    await test_cli_runner_gemini_mock_success()
    await test_cli_runner_claude_mock_success()
    await test_cli_runner_codex_mock_success()
    await test_cli_runner_antigravity_mock_success()
    await test_cli_runner_timeout()

    print("\n[6. 端到端集成 (mock AstrBot 全链路)]")
    await test_e2e_work_preflight_antigravity_success_route()
    await test_e2e_work_preflight_antigravity_failure_falls_back_to_aihubmix()
    await test_e2e_business_route()
    await test_e2e_simple_code_codex_cli_route()
    await test_e2e_dry_run_logs_but_does_not_switch()
    await test_e2e_classifier_uses_gemini_31_pro_when_rules_uncertain()
    await test_e2e_multimodal_preprocess_then_reroute()
    await test_e2e_ops_route_uses_codex_cli()
    await test_e2e_provider_missing_falls_back()
    await test_media_route_detection()
    await test_truth_intake_guard_respects_enabled_off()
    await test_truth_intake_guard_dry_run_writes_task_without_intercept()
    await test_truth_intake_guard_engine_unavailable_does_not_intercept()
    await test_truth_intake_blocks_company_fact_without_sources()
    await test_truth_intake_resumes_blocked_task_with_materials()
    await test_harness_state_injector_adds_truth_guard_without_active_tasks()
    await test_e2e_creative_insight_aihubmix_direct()
    await test_e2e_deep_routes_to_claude_opus()
    await test_queue_recovery_no_pending_jobs()
    await test_queue_recovery_skips_pending_gemini_cli_job()

    print("\n" + "=" * 60)
    print("✅ 全部测试通过")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
