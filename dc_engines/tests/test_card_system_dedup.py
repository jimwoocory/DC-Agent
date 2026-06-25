"""Tests for the dedup logic that powers ``runtime_events_recent`` health.

Pinned on 2026-06-11 after the user observed 3 production events with
``ok=false`` in ``data/card_runtime/events.jsonl`` triggering
``runtime_events_recent: FAILED``. Root cause: a streamer race produced
2-3 ``finalize`` attempts on the same ``message_id`` (first attempt
succeeded, retries fell through to ``plain_text``). The dedup function
folds those retries into a single failure so the threshold check
correctly reflects independent failures.

We do NOT mock the events file in these tests — we test the pure dedup
helper directly so the contract is locked independent of the jsonl
content.
"""

from __future__ import annotations

import pytest
from dc_engines.card_runtime import finalize_card_via_runtime, send_card_via_runtime
from dc_engines.card_system import (
    CardHealthReport,
    _dedupe_consecutive_failures,
    _deduped_production_card_failures,
    _production_card_runtime_events,
    build_sample_card,
    card_system_next_step,
)


class _FakeStreamer:
    def __init__(self) -> None:
        self.start_called = False
        self.finalize_called = False

    async def start(self, **_kwargs):
        self.start_called = True
        raise AssertionError("unregistered card_type must not call streamer.start")

    async def finalize(self, *_args, **_kwargs):
        self.finalize_called = True
        raise AssertionError("unregistered card_type must not call streamer.finalize")


class _FinalizeAndRetractStreamer:
    def __init__(self) -> None:
        self.scheduled_retracts: list[tuple[str, float]] = []

    async def finalize(self, _message_id: str, _card: dict) -> bool:
        return True

    def schedule_retract(self, message_id: str, delay_sec: float):
        self.scheduled_retracts.append((message_id, delay_sec))
        return object()


def _ev(
    ok: bool,
    message_id: str,
    event: str,
    *,
    card_type: str = "daily_response",
    platform_id: str = "巅池-Agent小助手",
    chat_id: str = "oc_test",
    receive_id_type: str = "chat_id",
    ts: str = "2026-06-11T00:00:00Z",
    detail: str = "",
) -> dict[str, object]:
    return {
        "ts": ts,
        "event": event,
        "card_type": card_type,
        "ok": ok,
        "platform_id": platform_id,
        "chat_id": chat_id,
        "receive_id_type": receive_id_type,
        "message_id": message_id,
        "detail": detail,
        "fallback": "" if ok else "plain_text",
    }


@pytest.mark.asyncio
async def test_runtime_send_rejects_unregistered_card_before_streamer_call() -> None:
    streamer = _FakeStreamer()

    with pytest.raises(KeyError, match="unknown card_type"):
        await send_card_via_runtime(
            streamer,
            card_type="not_registered_card",
            chat_id="oc_test",
            receive_id_type="chat_id",
            card={"elements": []},
        )

    assert streamer.start_called is False


@pytest.mark.asyncio
async def test_runtime_finalize_rejects_unregistered_card_before_streamer_call() -> (
    None
):
    streamer = _FakeStreamer()

    with pytest.raises(KeyError, match="unknown card_type"):
        await finalize_card_via_runtime(
            streamer,
            card_type="not_registered_card",
            message_id="om_test",
            card={"elements": []},
        )

    assert streamer.finalize_called is False


@pytest.mark.asyncio
async def test_runtime_finalize_can_schedule_retractable_task_card() -> None:
    streamer = _FinalizeAndRetractStreamer()

    ok = await finalize_card_via_runtime(
        streamer,
        card_type="media_generation",
        message_id="om_media_task",
        card={"elements": []},
        retract_after_sec=8.0,
    )

    assert ok is True
    assert streamer.scheduled_retracts == [("om_media_task", 8.0)]


class TestDedupeConsecutiveFailures:
    def test_empty_input_returns_empty(self) -> None:
        assert _dedupe_consecutive_failures([]) == []

    def test_all_ok_passes_through_unchanged(self) -> None:
        events = [
            _ev(True, "m1", "start"),
            _ev(True, "m2", "finalize"),
            _ev(True, "m3", "start"),
        ]
        assert _dedupe_consecutive_failures(events) == events

    def test_single_failure_kept(self) -> None:
        events = [_ev(False, "m1", "finalize", ts="2026-06-11T00:00:01Z")]
        assert _dedupe_consecutive_failures(events) == events

    def test_duplicate_failures_same_message_id_are_folded(self) -> None:
        """The exact production pattern: same message_id + same event
        type fired 2-3 times after the first success. We must keep only
        one so it counts as 1 failure, not 3."""
        events = [
            _ev(True, "m1", "start", ts="2026-06-11T00:00:00Z"),
            _ev(True, "m1", "finalize", ts="2026-06-11T00:00:11Z"),
            _ev(False, "m1", "finalize", ts="2026-06-11T00:00:21Z"),
            _ev(False, "m1", "finalize", ts="2026-06-11T00:00:48Z"),
        ]
        deduped = _dedupe_consecutive_failures(events)
        # First 2 ok events are kept as-is. Both failures on (m1, finalize)
        # fold into the first one. Result has 3 entries.
        assert len(deduped) == 3
        assert deduped[0]["ok"] is True
        assert deduped[1]["ok"] is True
        assert deduped[2]["ok"] is False
        assert deduped[2]["ts"] == "2026-06-11T00:00:21Z", (
            "we must keep the FIRST failure (chronologically earliest), "
            "not the latest — the latest is just a retry"
        )

    def test_failures_on_different_message_ids_each_count(self) -> None:
        """Independent failures (different message_ids) must NOT be folded —
        that would mask real systemic issues."""
        events = [
            _ev(False, "m1", "finalize", ts="2026-06-11T00:00:00Z"),
            _ev(False, "m2", "finalize", ts="2026-06-11T00:00:05Z"),
            _ev(False, "m3", "start", ts="2026-06-11T00:00:10Z"),
        ]
        deduped = _dedupe_consecutive_failures(events)
        assert len(deduped) == 3
        assert all(not item["ok"] for item in deduped)

    def test_failures_different_event_same_message_id_each_count(self) -> None:
        """Same message_id but different event type (start vs finalize) is
        a real second failure — streamer wrote both start and finalize
        with ok=false. We must not fold them."""
        events = [
            _ev(False, "m1", "start", ts="2026-06-11T00:00:00Z"),
            _ev(False, "m1", "finalize", ts="2026-06-11T00:00:01Z"),
        ]
        deduped = _dedupe_consecutive_failures(events)
        assert len(deduped) == 2

    def test_empty_message_id_treated_as_distinct_key(self) -> None:
        """``start`` events sometimes have empty message_id (the
        Feishu streamer hasn't received the message_id back from
        Feishu yet). Empty-message_id failures must not all fold
        into a single bucket — that would hide independent create failures."""
        events = [
            _ev(
                False,
                "",
                "start",
                chat_id="oc_first",
                ts="2026-06-11T00:00:00Z",
            ),
            _ev(
                False,
                "",
                "start",
                chat_id="oc_second",
                ts="2026-06-11T00:00:05Z",
            ),
        ]
        deduped = _dedupe_consecutive_failures(events)
        assert len(deduped) == 2

    def test_mixed_ok_and_failure_pattern_preserves_order(self) -> None:
        """Order is preserved (it's not a set). Useful for debugging
        the original timeline in case ops wants to look at the jsonl."""
        events = [
            _ev(True, "m1", "start", ts="2026-06-11T00:00:00Z"),
            _ev(False, "m1", "start", ts="2026-06-11T00:00:01Z"),
            _ev(True, "m1", "finalize", ts="2026-06-11T00:00:02Z"),
            _ev(False, "m1", "finalize", ts="2026-06-11T00:00:03Z"),
            _ev(False, "m1", "finalize", ts="2026-06-11T00:00:04Z"),
        ]
        deduped = _dedupe_consecutive_failures(events)
        # Failure (m1, start) folded with the prior ok (m1, start) — wait,
        # the prior is ok=True, only failures get deduped. So the ok
        # events are kept as-is, and the two (m1, finalize) failures
        # fold into one.
        assert len(deduped) == 4
        ok_count = sum(1 for item in deduped if item["ok"])
        fail_count = sum(1 for item in deduped if not item["ok"])
        assert ok_count == 2
        assert fail_count == 2

    def test_real_world_production_pattern_folds_3_into_2(self) -> None:
        """The exact 2026-06-10/11 production events:
        - 1 streamer race on ``om_x100b6da7564b4530c1e8e1e7b366877``
          (3 finalizes, 1 ok + 2 fails → folds to 1 fail)
        - 1 independent casual_reply short reply fallback (1 fail)
        - Total: 3 raw failures fold to 2 distinct failures.
        """
        events = [
            _ev(
                True,
                "om_x100b6da7564b4530c1e8e1e7b366877",
                "start",
                card_type="thinking_waiting",
                ts="2026-06-10T09:42:32Z",
            ),
            _ev(
                True,
                "om_x100b6da7564b4530c1e8e1e7b366877",
                "finalize",
                card_type="daily_response",
                ts="2026-06-10T09:42:43Z",
            ),
            _ev(
                False,
                "om_x100b6da7564b4530c1e8e1e7b366877",
                "finalize",
                card_type="daily_response",
                ts="2026-06-10T09:42:53Z",
            ),
            _ev(
                False,
                "om_x100b6da7564b4530c1e8e1e7b366877",
                "finalize",
                card_type="daily_response",
                ts="2026-06-10T09:43:20Z",
            ),
            _ev(
                False,
                "",
                "start",
                card_type="casual_reply",
                ts="2026-06-11T02:35:18Z",
            ),
        ]
        deduped = _dedupe_consecutive_failures(events)
        # Raw: 5 events, 3 ok=False
        # Dedup: 4 events, 2 ok=False (one fold for the streamer race)
        assert len(deduped) == 4
        distinct_failures = [item for item in deduped if not item["ok"]]
        assert len(distinct_failures) == 2
        # Verify it stays under the <= 3 threshold
        assert len(distinct_failures) <= 3


def test_webchat_card_events_are_not_production_runtime_failures() -> None:
    events = [
        _ev(
            False,
            "",
            "start",
            platform_id="webchat",
            chat_id="ou_smoke_user_1",
            receive_id_type="open_id",
        ),
        _ev(
            True,
            "om_real",
            "start",
            platform_id="巅池-Agent小助手",
            chat_id="oc_real",
        ),
    ]

    assert _production_card_runtime_events(events) == [events[1]]
    assert _deduped_production_card_failures(events) == []


def test_retried_grey_push_failure_is_cleared_by_later_success() -> None:
    events = [
        _ev(
            False,
            "",
            "grey_push",
            card_type="daily_response",
            chat_id="on_operator",
            receive_id_type="union_id",
            ts="2026-06-20T18:56:37Z",
            detail="font size heading_3 grey validation",
        ),
        _ev(
            True,
            "om_success",
            "grey_push",
            card_type="daily_response",
            chat_id="on_operator",
            receive_id_type="union_id",
            ts="2026-06-20T18:57:04Z",
            detail="font size heading_3 grey validation",
        ),
    ]

    assert _deduped_production_card_failures(events) == []


def test_regular_runtime_failure_is_not_cleared_by_later_success() -> None:
    events = [
        _ev(
            False,
            "",
            "start",
            card_type="daily_response",
            chat_id="oc_real",
            ts="2026-06-20T18:56:37Z",
            detail="daily renderer long response",
        ),
        _ev(
            True,
            "om_success",
            "start",
            card_type="daily_response",
            chat_id="oc_real",
            ts="2026-06-20T18:57:04Z",
            detail="daily renderer long response",
        ),
    ]

    assert _deduped_production_card_failures(events) == [events[0]]


def test_card_system_next_step_treats_retried_grey_push_as_green(monkeypatch) -> None:
    report = CardHealthReport(ok=True)
    report.add("runtime_events_recent", True, "ok")
    monkeypatch.setattr(
        "dc_engines.card_system.recent_card_runtime_events",
        lambda limit=30: [
            _ev(
                False,
                "",
                "grey_push",
                card_type="daily_response",
                chat_id="on_operator",
                receive_id_type="union_id",
                ts="2026-06-20T18:56:37Z",
                detail="font size heading_3 grey validation",
            ),
            _ev(
                True,
                "om_success",
                "grey_push",
                card_type="daily_response",
                chat_id="on_operator",
                receive_id_type="union_id",
                ts="2026-06-20T18:57:04Z",
                detail="font size heading_3 grey validation",
            ),
        ],
    )

    assert "runtime events are green" in card_system_next_step(report)


def test_card_system_next_step_ignores_non_production_webchat_failures(
    monkeypatch,
) -> None:
    report = CardHealthReport(ok=True)
    report.add("runtime_events_recent", True, "ok")
    monkeypatch.setattr(
        "dc_engines.card_system.recent_card_runtime_events",
        lambda limit=30: [
            _ev(
                False,
                "",
                "start",
                platform_id="webchat",
                chat_id="ou_smoke_user_1",
                receive_id_type="open_id",
            )
        ],
    )

    assert "fresh Feishu grey validation" in card_system_next_step(report)


def test_employee_insight_welcome_card_has_beginner_actions() -> None:
    card = build_sample_card("employee_insight_welcome")
    payload = str(card)

    assert "写通知" in payload
    assert "整理资料" in payload
    assert "生成汇报" in payload
    assert "我不知道怎么用" in payload
    assert "employee_insight_action" in payload
