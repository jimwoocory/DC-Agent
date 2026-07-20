from pathlib import Path
from unittest.mock import MagicMock

import pytest
from dc_engines.pet_live.service import PetLiveService
from dc_engines.pet_live.store import PetLiveStore

from data.plugins.feishu_pet_assistant import cards
from data.plugins.feishu_pet_assistant.main import FeishuPetAssistantPlugin
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


def test_status_card_prefers_live_state_when_available() -> None:
    live_state = {
        "name": "小橘",
        "state": "working",
        "level": 3,
        "xp": 42,
        "energy": 88,
        "coins": 5,
    }

    card = cards.build_status_card(
        {"pet_name": "旧小橘", "mood": "还行", "energy": 12},
        {"pending": 2, "done": 4},
        live_state=live_state,
    )
    body = card["elements"][0]["text"]["content"]
    text = cards.render_status_text(
        {"pet_name": "旧小橘", "mood": "还行", "energy": 12},
        {"pending": 2, "done": 4},
        live_state=live_state,
    )

    assert card["header"]["title"]["content"] == "小橘今天在等你"
    assert "Live 状态**：工作中" in body
    assert "等级 / XP**：Lv.3 / 42" in body
    assert "能量**：88 / 100" in body
    assert "金币**：5" in body
    assert "Live 状态：工作中" in text
    assert "等级 / XP：Lv.3 / 42" in text


def test_status_card_includes_desktop_entry_and_binding_status() -> None:
    card = cards.build_status_card(
        {"pet_name": "小橘"},
        {"pending": 0, "done": 1},
        live_state={"name": "小橘", "state": "idle", "pet_id": "pet_1"},
        desktop_url="https://dianchi.example/open-desktop?pet_id=pet_1",
        desktop_bound=False,
    )
    body = card["elements"][0]["text"]["content"]
    actions = card["elements"][2]["actions"]

    assert "桌面端**：待绑定" in body
    assert actions[1]["text"]["content"] == "打开桌面端"
    assert actions[1]["url"].endswith("pet_id=pet_1")


def test_plugin_url_template_replaces_user_and_pet_ids() -> None:
    url = FeishuPetAssistantPlugin._format_url_template(
        "https://dianchi.example/open?user={user_id}&pet={pet_id}",
        user_id="ou_user",
        pet_id="pet_1",
    )

    assert url == "https://dianchi.example/open?user=ou_user&pet=pet_1"


class _FakePetEvent:
    message_id = "om_1"
    session_id = "oc_1"
    message_str = ""

    def get_platform_name(self) -> str:
        return "lark"

    def get_platform_id(self) -> str:
        return "lark_main"


def test_plugin_records_live_event_with_feishu_source_ref(tmp_path: Path) -> None:
    live_store = PetLiveStore(tmp_path / "pet_live.db")
    plugin = FeishuPetAssistantPlugin.__new__(FeishuPetAssistantPlugin)
    plugin._live_service = PetLiveService(live_store)

    event = plugin._record_live_event(
        _FakePetEvent(),
        user_id="ou_user",
        event_type="feishu_message_received",
        payload={"text_len": 12},
    )

    assert event is not None
    assert event.source_ref.platform == "lark"
    assert event.source_ref.conversation_id == "oc_1"
    assert event.source_ref.message_id == "om_1"
    state = live_store.get_pet_state(event.pet_id)
    assert state is not None
    assert state.state == "waiting"


def test_plugin_live_event_respects_global_disable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("PET_LIVE_ENABLED", "false")
    live_store = PetLiveStore(tmp_path / "pet_live.db")
    plugin = FeishuPetAssistantPlugin.__new__(FeishuPetAssistantPlugin)
    plugin._live_service = PetLiveService(live_store)

    event = plugin._record_live_event(
        _FakePetEvent(),
        user_id="ou_user",
        event_type="feishu_message_received",
        payload={"text_len": 12},
    )

    assert event is None
    assert live_store.get_identity_by_feishu_open_id("ou_user") is None


@pytest.mark.asyncio
async def test_plugin_does_not_stop_other_card_action_sources() -> None:
    plugin = FeishuPetAssistantPlugin.__new__(FeishuPetAssistantPlugin)
    plugin._user_id = MagicMock(return_value="ou_user")
    plugin._parse_card_action = MagicMock(
        return_value={
            "value": {
                "source": "assistant_workbench",
                "action": "show_task",
            }
        }
    )
    plugin._record_live_event = MagicMock()
    event = MagicMock()

    await plugin.handle_card_action(event)

    event.stop_event.assert_not_called()
