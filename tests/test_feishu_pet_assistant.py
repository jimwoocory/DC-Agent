from pathlib import Path

import pytest

from data.plugins.feishu_pet_assistant import cards
from data.plugins.feishu_pet_assistant.service import PetService
from data.plugins.feishu_pet_assistant.store import PetStore


def _service(tmp_path: Path) -> tuple[PetService, PetStore]:
    store = PetStore(str(tmp_path / "pet.db"))
    return PetService(store), store


def test_new_pet_does_not_receive_seeded_tasks(tmp_path: Path) -> None:
    service, store = _service(tmp_path)

    pet = service.get_or_create_pet("ou_user")

    assert pet["user_id"] == "ou_user"
    assert store.list_tasks("ou_user") == []
    assert service.list_today_tasks("ou_user") == []
    assert service.build_stats("ou_user") == {"pending": 0, "done": 0}


def test_service_hides_and_blocks_legacy_non_production_tasks(
    tmp_path: Path,
) -> None:
    service, store = _service(tmp_path)
    service.get_or_create_pet("ou_user")
    legacy_task = store.insert_task("ou_user", "Legacy seeded task", source="demo")
    real_task = store.insert_task("ou_user", "真实客户回访", source="harness")

    visible_tasks = service.list_today_tasks("ou_user")

    assert [task["id"] for task in visible_tasks] == [real_task["id"]]
    assert service.complete_task("ou_user", legacy_task["id"]) is None

    completed = service.complete_first_pending("ou_user")

    assert completed is not None
    _, completed_task = completed
    assert completed_task["id"] == real_task["id"]
    assert service.build_stats("ou_user") == {"pending": 0, "done": 1}


@pytest.mark.parametrize(
    "source",
    ["demo_v1", "mock_seed", "sample_task", "placeholder_import", "stub.backfill"],
)
def test_service_blocks_non_production_source_variants(
    tmp_path: Path,
    source: str,
) -> None:
    service, store = _service(tmp_path)
    service.get_or_create_pet("ou_user")
    blocked_task = store.insert_task("ou_user", "Legacy imported task", source=source)

    assert service.list_today_tasks("ou_user") == []
    assert service.complete_task("ou_user", blocked_task["id"]) is None
    assert service.build_stats("ou_user") == {"pending": 0, "done": 0}


def test_empty_task_card_reports_missing_real_task_source() -> None:
    card = cards.build_tasks_card({"pet_name": "小橘"}, [])
    body = card["elements"][0]["text"]["content"]

    assert "真实任务源" in body
    assert card["header"]["title"]["content"] == "待办未接入"
    assert cards.render_tasks_text([]) == cards.NO_REAL_TASKS_TEXT
