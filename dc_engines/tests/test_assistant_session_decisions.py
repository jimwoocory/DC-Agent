from __future__ import annotations

from pathlib import Path

import pytest
from dc_engines.harness import HarnessTaskStore


async def _store(tmp_path: Path) -> HarnessTaskStore:
    store = HarnessTaskStore(tmp_path / "harness.db")
    await store.initialize()
    return store


async def test_session_decision_is_unique_and_first_choice_wins(
    tmp_path: Path,
) -> None:
    store = await _store(tmp_path)
    decision, previous, created = await store.create_session_decision(
        task_id="task-1",
        unified_msg_origin="lark:FriendMessage:ou_test",
        platform_id="lark-bot",
        chat_id="ou_test",
        source_conversation_id="conv-old",
    )

    assert created is True
    assert previous is None
    assert decision.state == "pending"

    replay, previous, created = await store.create_session_decision(
        task_id="task-1",
        unified_msg_origin="lark:FriendMessage:ou_test",
        platform_id="lark-bot",
        chat_id="ou_test",
        source_conversation_id="conv-old",
    )
    assert created is False
    assert previous is None
    assert replay.decision_id == decision.decision_id

    continued = await store.continue_session_decision(
        decision.decision_id,
        operator_id="ou_test",
        decision_source="card_click",
    )
    assert continued is not None
    assert continued.state == "continue_current"
    assert continued.target_conversation_id == "conv-old"

    replayed_new = await store.begin_new_session_decision(
        decision.decision_id,
        operator_id="ou_test",
    )
    assert replayed_new is not None
    assert replayed_new.state == "continue_current"


async def test_new_pending_decision_supersedes_old_card_atomically(
    tmp_path: Path,
) -> None:
    store = await _store(tmp_path)
    first, _, _ = await store.create_session_decision(
        task_id="task-1",
        unified_msg_origin="lark:FriendMessage:ou_test",
        platform_id="lark-bot",
        chat_id="ou_test",
        source_conversation_id="conv-old",
    )
    await store.bind_session_decision_card(first.decision_id, "om_first")

    second, previous, created = await store.create_session_decision(
        task_id="task-2",
        unified_msg_origin="lark:FriendMessage:ou_test",
        platform_id="lark-bot",
        chat_id="ou_test",
        source_conversation_id="conv-old",
    )

    assert created is True
    assert second.state == "pending"
    assert previous is not None
    assert previous.decision_id == first.decision_id
    assert previous.state == "superseded_continue"
    persisted = await store.get_session_decision(first.decision_id)
    assert persisted is not None
    assert persisted.state == "superseded_continue"
    assert (
        await store.get_pending_session_decision("lark:FriendMessage:ou_test") == second
    )


async def test_opening_new_decision_recovers_to_one_terminal_target(
    tmp_path: Path,
) -> None:
    store = await _store(tmp_path)
    decision, _, _ = await store.create_session_decision(
        task_id="task-1",
        unified_msg_origin="lark:FriendMessage:ou_test",
        platform_id="lark-bot",
        chat_id="ou_test",
        source_conversation_id="conv-old",
    )

    opening = await store.begin_new_session_decision(
        decision.decision_id,
        operator_id="ou_test",
    )
    assert opening is not None
    assert opening.state == "opening_new"

    completed = await store.complete_new_session_decision(
        decision.decision_id,
        target_conversation_id="conv-new",
    )
    assert completed is not None
    assert completed.state == "new_conversation"
    assert completed.target_conversation_id == "conv-new"

    replay = await store.complete_new_session_decision(
        decision.decision_id,
        target_conversation_id="conv-duplicate",
    )
    assert replay is not None
    assert replay.target_conversation_id == "conv-new"


@pytest.mark.parametrize("patch_state", ["pending", "synced", "failed"])
async def test_card_patch_state_is_auditable(
    tmp_path: Path,
    patch_state: str,
) -> None:
    store = await _store(tmp_path)
    decision, _, _ = await store.create_session_decision(
        task_id=f"task-{patch_state}",
        unified_msg_origin="lark:FriendMessage:ou_test",
        platform_id="lark-bot",
        chat_id="ou_test",
        source_conversation_id="conv-old",
    )

    updated = await store.mark_session_decision_card_patch(
        decision.decision_id,
        patch_state,
    )
    assert updated is not None
    assert updated.card_patch_state == patch_state
