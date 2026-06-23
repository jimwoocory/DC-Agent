from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from data.plugins.dc_router import truth_intake


class _Event:
    def __init__(self, text: str) -> None:
        self.message_str = text
        self.unified_msg_origin = "巅池-Agent小助手:FriendMessage:ou_test"
        self.message_obj = SimpleNamespace(message=[], message_str=text)

    def get_platform_id(self) -> str:
        return "巅池-Agent小助手"

    def get_sender_id(self) -> str:
        return "ou_test"


@pytest.mark.asyncio
async def test_truth_intake_dry_run_does_not_create_blocked_task() -> None:
    """Dry-run must observe only; it must not pollute Harness with intake tasks."""

    event = _Event("帮我整理一下这个项目的数据报告，看看有没有风险。")
    store = SimpleNamespace(list_tasks_for_session=AsyncMock(return_value=[]))
    engine = SimpleNamespace(
        create_task=AsyncMock(side_effect=AssertionError("dry-run side effect"))
    )
    ctx = SimpleNamespace(harness_store=store, harness_engine=engine)

    handled = await truth_intake.maybe_handle_truth_intake(
        ctx,
        event,
        {"enabled": True, "dry_run": True},
    )

    assert handled is False
    store.list_tasks_for_session.assert_awaited_once()
    engine.create_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_truth_intake_dry_run_does_not_resume_or_mutate_blocked_task() -> None:
    """Supplying source material in dry-run must not append traces or mark tasks."""

    event = _Event("补充：这是项目背景材料，客户要明早之前拿到最终报告。")
    blocked_task = SimpleNamespace(
        task_id="abcdef123456",
        domain=truth_intake.INTAKE_DOMAIN,
        status="blocked",
        payload={
            "source": truth_intake.INTAKE_SOURCE,
            "original_text": "帮我做项目报告",
        },
    )
    store = SimpleNamespace(
        list_tasks_for_session=AsyncMock(return_value=[blocked_task])
    )
    engine = SimpleNamespace(
        append_trace=AsyncMock(side_effect=AssertionError("dry-run append_trace")),
        mark_in_progress=AsyncMock(
            side_effect=AssertionError("dry-run mark_in_progress")
        ),
    )
    ctx = SimpleNamespace(harness_store=store, harness_engine=engine)

    handled = await truth_intake.maybe_handle_truth_intake(
        ctx,
        event,
        {"enabled": True, "dry_run": True},
    )

    assert handled is False
    store.list_tasks_for_session.assert_awaited_once()
    engine.append_trace.assert_not_awaited()
    engine.mark_in_progress.assert_not_awaited()


@pytest.mark.asyncio
async def test_truth_intake_dry_run_missing_materials_log_does_not_imply_task_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dry-run logs must not imply a new/real task exists when only observing."""

    event = _Event("帮我整理一下上周活动数据，给一份能直接发给执行部的风险报告。")
    blocked_task = SimpleNamespace(
        task_id="abcdef123456",
        domain=truth_intake.INTAKE_DOMAIN,
        status="blocked",
        payload={
            "source": truth_intake.INTAKE_SOURCE,
            "original_text": "帮我做项目报告",
        },
    )
    store = SimpleNamespace(
        list_tasks_for_session=AsyncMock(return_value=[blocked_task])
    )
    engine = SimpleNamespace(
        create_task=AsyncMock(side_effect=AssertionError("dry-run side effect"))
    )
    ctx = SimpleNamespace(harness_store=store, harness_engine=engine)
    log_messages: list[str] = []

    def capture_info(message: str, *args: object) -> None:
        log_messages.append(message % args if args else message)

    monkeypatch.setattr(truth_intake.logger, "info", capture_info)

    handled = await truth_intake.maybe_handle_truth_intake(
        ctx,
        event,
        {"enabled": True, "dry_run": True},
    )

    assert handled is False
    engine.create_task.assert_not_awaited()
    assert any("task=-" in message for message in log_messages)
    assert all("abcdef12" not in message for message in log_messages)
