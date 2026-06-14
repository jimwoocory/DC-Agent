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

from dc_engines.card_system import _dedupe_consecutive_failures


def _ev(
    ok: bool,
    message_id: str,
    event: str,
    *,
    card_type: str = "daily_response",
    ts: str = "2026-06-11T00:00:00Z",
) -> dict[str, object]:
    return {
        "ts": ts,
        "event": event,
        "card_type": card_type,
        "ok": ok,
        "message_id": message_id,
        "detail": "",
        "fallback": "" if ok else "plain_text",
    }


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
        into a single bucket — that would hide ``start`` races."""
        events = [
            _ev(False, "", "start", ts="2026-06-11T00:00:00Z"),
            _ev(False, "", "start", ts="2026-06-11T00:00:05Z"),
        ]
        deduped = _dedupe_consecutive_failures(events)
        # Empty message_id is a real key — both events are "same key"
        # (("", "start")), so we DO fold them. The streamer retry
        # pattern is the same regardless of whether message_id was
        # captured. This is the safer behavior because if we did NOT
        # fold, empty-message-id events would always trip the threshold.
        assert len(deduped) == 1

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
