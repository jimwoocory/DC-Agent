from __future__ import annotations

from types import SimpleNamespace

import pytest

from astrbot.core.provider.entities import ProviderRequest
from data.plugins.harness_state_injector.main import HarnessStateInjectorPlugin


class _FakeHarnessStore:
    async def list_tasks_for_session(self, session_id, limit, statuses):
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
    context = SimpleNamespace(harness_store=_FakeHarnessStore())
    plugin = HarnessStateInjectorPlugin(context)
    event = SimpleNamespace(
        message_str="需要",
        unified_msg_origin="巅池-Agent小助手:FriendMessage:ou_user",
        get_platform_id=lambda: "巅池-Agent小助手",
    )
    req = ProviderRequest(system_prompt="base")

    await plugin.inject_active_tasks(event, req)

    assert "source_archive_dir" in req.system_prompt
    assert "柳汽Q2视频选题_7月_.docx" in req.system_prompt
    assert "优先读取上述 source_attachment" in req.system_prompt
    assert "不要把 data/temp 里的历史导入" in req.system_prompt
