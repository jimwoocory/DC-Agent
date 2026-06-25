from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from data.plugins.dc_router import truth_intake


class _Event:
    def __init__(self, text: str) -> None:
        self.message_str = text
        self.unified_msg_origin = "巅池-Agent小助手:FriendMessage:ou_test"
        self.message_obj = SimpleNamespace(message=[], message_str=text)
        self.extras: dict[str, str] = {}

    def get_platform_id(self) -> str:
        return "巅池-Agent小助手"

    def get_sender_id(self) -> str:
        return "ou_test"

    def set_extra(self, key: str, value: str) -> None:
        self.extras[key] = value


@pytest.mark.asyncio
async def test_public_planning_market_research_bypasses_truth_intake() -> None:
    """Public market research should reach router search, not material blocking."""

    event = _Event(
        "帮我做一份五菱缤果近期新能源小车竞品市场调研报告，"
        "需要结合最新公开信息，列出主要竞品、卖点对比、传播机会和策划建议。"
        "涉及数据和外部事实请标明来源。"
    )
    store = SimpleNamespace(
        list_tasks_for_session=AsyncMock(side_effect=AssertionError("should bypass"))
    )
    ctx = SimpleNamespace(harness_store=store)

    handled = await truth_intake.maybe_handle_truth_intake(
        ctx,
        event,
        {"enabled": True, "dry_run": False},
    )

    assert handled is False
    assert event.extras["dc_truth_intake_bypass"] == "public_planning_research"
    store.list_tasks_for_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_private_market_research_materials_still_use_truth_intake() -> None:
    """Private company facts must not be hidden behind the public-search bypass."""

    event = _Event(
        "帮我做一个竞品市场调研报告，根据内部销售数据和飞书文件分析机会。"
    )
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
    assert "dc_truth_intake_bypass" not in event.extras
    store.list_tasks_for_session.assert_awaited_once()
    engine.create_task.assert_not_awaited()


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


def test_truth_source_context_does_not_mutate_user_message(tmp_path: Path) -> None:
    original = "帮我整理项目报告"
    source = "补充：这是项目真实背景材料。"
    event = _Event(source)
    archive = truth_intake.IntakeArchive(
        intake_id="intake123",
        intake_dir=tmp_path / "intake123",
        text_path=tmp_path / "intake123" / "source.txt",
        attachments=[],
    )

    truth_intake._augment_event_with_source(
        event,
        original_text=original,
        source_text=source,
        archive=archive,
        task_id="task123",
    )

    assert event.message_str == source
    assert event.message_obj.message_str == source
    assert "<dc_truth_source" in event.extras["dc_truth_source_context"]
    assert "dc_truth_source_context" in event.extras
