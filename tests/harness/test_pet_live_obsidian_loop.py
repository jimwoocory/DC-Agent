from pathlib import Path

from dc_engines.memory_governance.models import GovernedMemory
from dc_engines.pet_live.identity import get_or_create_identity
from dc_engines.pet_live.obsidian_bridge import (
    record_memory_promoted,
    record_obsidian_note_linked,
)
from dc_engines.pet_live.store import PetLiveStore


def test_pet_live_obsidian_bridge_records_note_and_promotion(
    tmp_path: Path,
) -> None:
    store = PetLiveStore(tmp_path / "pet_live.db")
    identity = get_or_create_identity(store, feishu_open_id="ou_user")

    note_event = record_obsidian_note_linked(
        store,
        pet_id=identity.pet_id,
        user_id="ou_user",
        obsidian_note_path="ObsidianVault/40_MemoryGovernance/Inbox/task.md",
        harness_task_id="task_1",
        memory_id="mem_1",
    )
    promoted_event = record_memory_promoted(
        store,
        pet_id=identity.pet_id,
        user_id="ou_user",
        obsidian_note_path="ObsidianVault/40_MemoryGovernance/Inbox/task.md",
        kb_doc_id="kb_1",
        harness_task_id="task_1",
        memory_id="mem_1",
    )

    events = store.list_events_after(identity.pet_id, after_id=0)
    state = store.get_pet_state(identity.pet_id)

    assert [event.event_type for event in events] == [
        "obsidian_note_linked",
        "memory_promoted",
    ]
    assert note_event.source_ref.obsidian_note_path.endswith("task.md")
    assert promoted_event.source_ref.kb_doc_id == "kb_1"
    assert promoted_event.payload["memory_id"] == "mem_1"
    assert state is not None
    assert state.state == "happy"


def test_governed_memory_accepts_pet_live_source_system() -> None:
    memory = GovernedMemory(
        memory_id="mem_pet_1",
        source_system="pet_live",
        source_id="event_1",
        source_path="pet_event_outbox:1",
        source_hash="hash_1",
        title="宠物任务成果",
        summary="任务完成后沉淀为记忆。",
        canonical_text="项目任务完成，结果已进入治理流程。",
        memory_kind="project",
        review_status="need_review",
        confidence=0.8,
        sensitivity="internal",
    )

    assert memory.source_system == "pet_live"
