from __future__ import annotations

from types import SimpleNamespace

import pytest

from astrbot.core.provider.entities import ProviderRequest
from data.plugins.harness_state_injector.main import HarnessStateInjectorPlugin


class _FakeHarnessStore:
    def __init__(self) -> None:
        self.calls = 0

    async def list_tasks_for_session(self, session_id, limit, statuses):
        self.calls += 1
        return [
            SimpleNamespace(
                task_id="abcdef123456",
                status="in_progress",
                domain="truth_intake",
                created_at="2026-06-03T08:50:44+00:00",
                title="真实性资料校验：柳汽Q2视频选题",
                payload={
                    "source": "llm_router_truth_intake",
                    "brief": "柳汽Q2视频选题（7月）",
                    "archive_dir": "/Users/dianchi/DC-Agent/data/harness_intake/raw/20260603/intake123",
                    "attachments": [
                        {
                            "original_name": "柳汽Q2视频选题（7月） .docx",
                            "stored_path": "/Users/dianchi/DC-Agent/data/harness_intake/raw/20260603/intake123/attachments/柳汽Q2视频选题_7月_.docx",
                            "kind": "file",
                        }
                    ],
                },
            )
        ][:limit]


@pytest.mark.asyncio
async def test_truth_intake_task_injection_includes_source_material_paths():
    store = _FakeHarnessStore()
    context = SimpleNamespace(harness_store=store)
    plugin = HarnessStateInjectorPlugin(context)
    event = SimpleNamespace(
        message_str="这个任务进度怎么样？",
        unified_msg_origin="巅池-Agent小助手:FriendMessage:ou_user",
        get_platform_id=lambda: "巅池-Agent小助手",
        get_extra=lambda _key: "",
    )
    req = ProviderRequest(system_prompt="base")

    await plugin.inject_active_tasks(event, req)

    assert store.calls == 1
    assert "source_archive_dir" in req.system_prompt
    assert "柳汽Q2视频选题_7月_.docx" in req.system_prompt
    assert "优先读取上述 source_attachment" in req.system_prompt
    assert "不要把 data/temp 里的历史导入" in req.system_prompt


@pytest.mark.asyncio
async def test_harness_state_injector_skips_casual_chat_to_avoid_stale_context():
    store = _FakeHarnessStore()
    context = SimpleNamespace(harness_store=store)
    plugin = HarnessStateInjectorPlugin(context)
    event = SimpleNamespace(
        message_str="直接问吧",
        unified_msg_origin="巅池-Agent小助手:FriendMessage:ou_user",
        get_platform_id=lambda: "巅池-Agent小助手",
        get_extra=lambda key: "casual" if key == "dc_router_intent" else "",
    )
    req = ProviderRequest(system_prompt="base")

    await plugin.inject_active_tasks(event, req)

    assert store.calls == 0
    assert req.system_prompt.startswith("base")
    assert "DC-Agent 真实性铁律" in req.system_prompt
    assert "Harness 任务状态约束" not in req.system_prompt
    assert "abcdef12" not in req.system_prompt
