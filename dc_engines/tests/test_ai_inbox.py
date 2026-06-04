from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from dc_engines.ai_inbox import AIInboxEngine, InboxItemCreateRequest, InboxStore


def _load_ai_inbox_plugin_module():
    module_path = (
        Path(__file__).resolve().parents[2]
        / "data"
        / "plugins"
        / "ai_inbox_plugin"
        / "main.py"
    )
    spec = importlib.util.spec_from_file_location(
        "dc_ai_inbox_plugin_test",
        module_path,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _FakeContext:
    def get_config(self):
        return {}


class _FakeEvent:
    unified_msg_origin = "s1"

    def __init__(self, extras: dict[str, str]) -> None:
        self.extras = extras

    def get_extra(self, key: str):
        return self.extras.get(key)


class _FakeLarkEventWithoutIsGroup:
    unified_msg_origin = "巅池-Agent小助手:FriendMessage:ou_test"

    def __init__(self, group_id: str = "", at: bool = False) -> None:
        self._group_id = group_id
        self.is_at_or_wake_command = at

    def get_sender_id(self):
        return "ou_user"

    def get_self_id(self):
        return "ou_bot"

    def get_platform_id(self):
        return "巅池-Agent小助手"

    def get_platform_name(self):
        return "lark"

    def get_group_id(self):
        return self._group_id


class _FakeReviewEvent:
    unified_msg_origin = "巅池-Agent小助手:FriendMessage:ou_user"

    def __init__(self, memory_context: dict) -> None:
        self._memory_context = memory_context

    def get_extra(self, key: str):
        if key == "dc_agent_memory_context":
            return self._memory_context
        return None

    def get_sender_id(self):
        return "ou_user"

    def get_sender_name(self):
        return "玉晓莉"

    def get_platform_id(self):
        return "巅池-Agent小助手"


async def test_inbox_create_link_and_close(tmp_path: Path) -> None:
    store = InboxStore(tmp_path / "ai_inbox.db")
    await store.initialize()
    engine = AIInboxEngine(store)

    item = await engine.create_item(
        InboxItemCreateRequest(
            session_id="lark:group:1",
            conversation_id="conv_1",
            platform_id="巅池-Agent小助手",
            sender_id="ou_1",
            sender_name="张三",
            text="麻烦提醒我明天交方案",
            category=engine.classify("麻烦提醒我明天交方案"),
            case_id="case_1",
        )
    )

    assert item.category == "task"
    assert item.status == "new"

    linked = await store.update_item(
        item.item_id,
        status="in_progress",
        task_id="task_1",
        event_type="task_linked",
    )
    assert linked.task_id == "task_1"
    assert linked.case_id == "case_1"

    found = await store.find_by_task_id("task_1")
    assert found is not None
    assert found.item_id == item.item_id

    closed = await store.update_item(
        item.item_id,
        status="closed",
        event_type="task_closed",
    )
    assert closed.status == "closed"
    events = await store.list_events(item.item_id)
    assert [event.event_type for event in events] == [
        "item_created",
        "task_linked",
        "task_closed",
    ]


async def test_inbox_stats_and_latest_open(tmp_path: Path) -> None:
    store = InboxStore(tmp_path / "ai_inbox.db")
    await store.initialize()
    engine = AIInboxEngine(store)

    first = await engine.create_item(
        InboxItemCreateRequest(
            session_id="s1",
            conversation_id="c1",
            platform_id="p",
            sender_id="u",
            sender_name="u",
            text="这个结果不够好",
            category=engine.classify("这个结果不够好"),
        )
    )
    second = await engine.create_item(
        InboxItemCreateRequest(
            session_id="s1",
            conversation_id="c1",
            platform_id="p",
            sender_id="u",
            sender_name="u",
            text="补充一个飞书文档链接",
            category=engine.classify("补充一个飞书文档链接"),
            status="waiting_materials",
        )
    )
    await store.update_item(first.item_id, status="closed")

    latest = await store.find_latest_open_item("s1")
    assert latest is not None
    assert latest.item_id == second.item_id
    assert latest.category == "material"

    stats = await store.stats()
    assert {"status": "closed", "count": 1} in stats["status_counts"]
    assert {"status": "waiting_materials", "count": 1} in stats["status_counts"]
    assert {"category": "feedback", "count": 1} in stats["category_counts"]
    assert {"category": "material", "count": 1} in stats["category_counts"]


async def test_plugin_link_task_updates_existing_and_current_items(
    tmp_path: Path,
) -> None:
    module = _load_ai_inbox_plugin_module()
    store = InboxStore(tmp_path / "ai_inbox.db")
    await store.initialize()
    plugin = module.AIInboxPlugin(_FakeContext())
    plugin.store = store

    original = await store.create_item(
        InboxItemCreateRequest(
            session_id="s1",
            conversation_id="c1",
            platform_id="p",
            sender_id="u1",
            sender_name="u1",
            text="请基于公司真实资料写一份方案",
            category="request",
            status="waiting_materials",
            case_id="case_1",
            task_id="task_1",
        )
    )
    current = await store.create_item(
        InboxItemCreateRequest(
            session_id="s1",
            conversation_id="c1",
            platform_id="p",
            sender_id="u1",
            sender_name="u1",
            text="补充飞书文档链接",
            category="material",
            status="acknowledged",
            case_id="case_1",
        )
    )

    event = _FakeEvent(
        {
            "ai_inbox_item_id": current.item_id,
            "ai_inbox_case_id": "case_1",
        }
    )
    await plugin.link_task_for_event(
        event,
        "task_1",
        status="in_progress",
        source="test",
    )

    updated_original = await store.get_item(original.item_id)
    updated_current = await store.get_item(current.item_id)
    assert updated_original is not None
    assert updated_current is not None
    assert updated_original.status == "in_progress"
    assert updated_current.status == "in_progress"
    assert updated_original.task_id == "task_1"
    assert updated_current.task_id == "task_1"


def test_plugin_should_track_lark_private_event_without_is_group_method() -> None:
    module = _load_ai_inbox_plugin_module()
    plugin = module.AIInboxPlugin(_FakeContext())

    event = _FakeLarkEventWithoutIsGroup()

    assert plugin._is_group_event(event) is False
    assert plugin._should_track(event, "怎么拉你进群") is True


def test_plugin_group_event_without_is_group_requires_at_command() -> None:
    module = _load_ai_inbox_plugin_module()
    plugin = module.AIInboxPlugin(_FakeContext())

    event = _FakeLarkEventWithoutIsGroup(group_id="oc_group", at=False)

    assert plugin._is_group_event(event) is True
    assert plugin._should_track(event, "普通群聊消息") is False


async def test_plugin_records_obsidian_review_reply(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_ai_inbox_plugin_module()
    store = InboxStore(tmp_path / "ai_inbox.db")
    await store.initialize()
    plugin = module.AIInboxPlugin(_FakeContext())
    plugin.store = store

    item = await store.create_item(
        InboxItemCreateRequest(
            session_id="s1",
            conversation_id="c1",
            platform_id="巅池-Agent小助手",
            sender_id="ou_user",
            sender_name="玉晓莉",
            text="需要调整：\n- 负责人：玉晓莉",
            category="request",
        )
    )
    records = []
    monkeypatch.setattr(module, "append_review_record", records.append)
    event = _FakeReviewEvent(
        {
            "documents": [
                {
                    "doc_key": "doc_1",
                    "title": "星光S",
                    "rel_path": "星光S.xlsx",
                    "review_status": "need_review",
                }
            ]
        }
    )

    await plugin._maybe_record_obsidian_review(
        event,
        item.item_id,
        "需要调整：\n- 负责人：玉晓莉",
    )

    updated = await store.get_item(item.item_id)
    assert len(records) == 1
    assert records[0].parsed_fields["owner"] == "玉晓莉"
    assert updated is not None
    assert updated.payload["obsidian_review_id"] == records[0].review_id
    assert updated.payload["obsidian_review_candidates"] == ["doc_1"]
