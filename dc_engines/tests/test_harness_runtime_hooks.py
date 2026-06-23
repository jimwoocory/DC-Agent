from __future__ import annotations

from types import SimpleNamespace

from dc_engines.harness.runtime_hooks import (
    HarnessSensorRuntime,
    HarnessStateInjectionRuntime,
    classify_response_quality,
)


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
                    "archive_dir": "/tmp/intake123",
                    "attachments": [
                        {
                            "original_name": "柳汽Q2视频选题（7月）.docx",
                            "stored_path": "/tmp/intake123/attachments/topic.docx",
                        }
                    ],
                },
            )
        ][:limit]


class _FakeProviderRequest:
    def __init__(self) -> None:
        self.system_prompt = "base"


async def test_state_injection_runtime_injects_truth_intake_materials() -> None:
    store = _FakeHarnessStore()
    runtime = HarnessStateInjectionRuntime(SimpleNamespace(harness_store=store))
    event = SimpleNamespace(
        message_str="这个任务做完了吗？",
        unified_msg_origin="session-1",
        get_platform_id=lambda: "巅池-Agent小助手",
        get_extra=lambda _key: "",
    )
    req = _FakeProviderRequest()

    await runtime.inject_active_tasks(event, req)

    assert store.calls == 1
    assert "source_archive_dir: /tmp/intake123" in req.system_prompt
    assert "source_attachment: 柳汽Q2视频选题（7月）.docx" in req.system_prompt
    assert "不要把 data/temp 里的历史导入" in req.system_prompt


def test_sensor_runtime_classification_stays_reusable_outside_plugin() -> None:
    assert (
        classify_response_quality(None, "资料不足，需要补充来源")
        == "insufficient_materials"
    )
    assert (
        classify_response_quality(None, "已完成，未完成项：无；本次资料无需补充。")
        == "success"
    )


async def test_sensor_runtime_does_not_fallback_without_event_task_id() -> None:
    runtime = HarnessSensorRuntime(SimpleNamespace())
    store = SimpleNamespace(get_task=lambda _task_id: None)
    engine = SimpleNamespace(store=store)

    assert (
        await runtime.load_target_tasks(
            SimpleNamespace(get_extra=lambda _key: None), engine
        )
        == []
    )
