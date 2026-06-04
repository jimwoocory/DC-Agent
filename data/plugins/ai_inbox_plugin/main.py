"""AI Inbox plugin.

The inbox is the communication bridge above tools and Harness tasks:
employee messages become visible intake records, and actionable requests get
a session-level Case automatically so later tasks have somewhere to attach.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from dc_engines.ai_inbox import (
    AIInboxEngine,
    InboxItem,
    InboxItemCreateRequest,
    InboxStore,
)
from dc_engines.employee_directory import requester_meta_from_event
from dc_engines.obsidian_review import (
    append_review_record,
    build_review_record,
    looks_like_obsidian_review_reply,
)

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register

_TRACKED_PLATFORM_IDS = {
    "巅池-Agent小助手",
    "巅池-技术（DevOps）",
    "巅池-技术",
    "巅池-推广01",
    "巅池-推广 01",
    "巅池1号",
}

_COMMANDS_TO_IGNORE = (
    "/employees",
    "employees ",
    "/case list",
    "/case context",
    "/task ls",
    "/task show",
    "/task start",
    "/task done",
    "/task approve",
    "/task reject",
    "/tasks",
    "tasks",
)


@register(
    "ai_inbox_plugin",
    "dc_agent",
    "AI Inbox：员工请求收件箱 + Case 自动承接（组织沟通桥梁 P0）",
    "0.1.0",
)
class AIInboxPlugin(Star):
    def __init__(self, context: Context) -> None:
        super().__init__(context)
        self.store: InboxStore | None = None
        self.engine: AIInboxEngine | None = None

    async def initialize(self) -> None:
        data_dir = Path(__file__).resolve().parents[3] / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        self.store = InboxStore(data_dir / "ai_inbox.db")
        await self.store.initialize()
        self.engine = AIInboxEngine(self.store)

        self.context.ai_inbox_store = self.store
        self.context.ai_inbox_engine = self.engine
        self.context.ai_inbox_ensure_case = self.ensure_case_for_event
        self.context.ai_inbox_link_task = self.link_task_for_event

        try:
            self.context.register_web_api(
                "/ai_inbox/items",
                self._api_items,
                ["GET"],
                "AI Inbox 最近员工请求",
            )
            self.context.register_web_api(
                "/ai_inbox/stats",
                self._api_stats,
                ["GET"],
                "AI Inbox 状态与类别统计",
            )
            logger.info("[ai_inbox] API 已注册：/api/plug/ai_inbox/{items,stats}")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[ai_inbox] 注册 API 失败：%s", exc)

        logger.info("[ai_inbox] InboxStore 启动：%s", data_dir / "ai_inbox.db")

    @filter.event_message_type(
        EventMessageType.GROUP_MESSAGE | EventMessageType.PRIVATE_MESSAGE,
        priority=80,
    )
    async def on_message(self, event: AstrMessageEvent):
        if self.engine is None:
            return
        text = self._extract_text(event)
        if not self._should_track(event, text):
            return

        category = self.engine.classify(text)
        conversation_id = await self._conversation_id(event)
        case_id = ""
        if self.engine.is_actionable(category):
            case_id = await self.ensure_case_for_event(
                event,
                category=category,
                text=text,
            )

        item = await self.engine.create_item(
            InboxItemCreateRequest(
                session_id=event.unified_msg_origin or "",
                conversation_id=conversation_id,
                platform_id=event.get_platform_id() or "",
                sender_id=str(event.get_sender_id() or ""),
                sender_name=event.get_sender_name() or str(event.get_sender_id() or ""),
                text=text[:4000],
                category=category,
                status="acknowledged" if case_id else "new",
                case_id=case_id,
                payload={
                    "is_group": self._is_group_event(event),
                    "message_id": str(getattr(event, "message_id", "") or ""),
                    "is_at_or_wake_command": bool(
                        getattr(event, "is_at_or_wake_command", False)
                    ),
                },
            )
        )
        await self._maybe_record_obsidian_review(event, item.item_id, text)
        event.set_extra("ai_inbox_item_id", item.item_id)
        if case_id:
            event.set_extra("ai_inbox_case_id", case_id)
        logger.debug(
            "[ai_inbox] item=%s category=%s case=%s session=%s",
            item.item_id[:8],
            category,
            case_id[:8] if case_id else "-",
            (event.unified_msg_origin or "")[:80],
        )

    async def ensure_case_for_event(
        self,
        event: AstrMessageEvent,
        *,
        category: str = "request",
        text: str = "",
        task_id: str = "",
    ) -> str:
        case_engine = getattr(self.context, "case_engine", None)
        if case_engine is None:
            return ""

        try:
            active = await case_engine.get_current_case_for_session(
                event.unified_msg_origin
            )
            if active is not None:
                return active.case_id
        except Exception as exc:  # noqa: BLE001
            logger.debug("[ai_inbox] 查询 active case 失败：%s", exc)
            return ""

        sender_name = event.get_sender_name() or str(event.get_sender_id() or "")
        name_seed = self._case_name_seed(sender_name, text)
        payload = {
            "source": "ai_inbox_plugin",
            "auto_created": True,
            "category": category,
            "trigger_text": text[:300],
            "trigger_sender_id": str(event.get_sender_id() or ""),
            "trigger_sender_name": sender_name,
        }
        payload.update(await requester_meta_from_event(self.context, event))
        if task_id:
            payload["first_task_id"] = task_id
        try:
            case = await case_engine.create_case(
                name=name_seed,
                platform_id=event.get_platform_id() or "",
                session_id=event.unified_msg_origin or "",
                payload=payload,
            )
            logger.info(
                "[ai_inbox] 自动创建 Case case=%s category=%s sender=%s",
                case.case_id[:8],
                category,
                sender_name,
            )
            return case.case_id
        except Exception as exc:  # noqa: BLE001
            logger.warning("[ai_inbox] 自动创建 Case 失败：%s", exc)
            return ""

    async def link_task_for_event(
        self,
        event: AstrMessageEvent,
        task_id: str,
        *,
        status: str = "in_progress",
        case_id: str = "",
        source: str = "",
    ) -> None:
        if self.store is None or not task_id:
            return

        item_id = str(event.get_extra("ai_inbox_item_id") or "")
        item: InboxItem | None = None
        linked_item: InboxItem | None = None
        try:
            linked_item = await self.store.find_by_task_id(task_id)
        except Exception:  # noqa: BLE001
            linked_item = None

        if item_id:
            try:
                item = await self.store.get_item(item_id)
            except Exception:  # noqa: BLE001
                item = None
        if item is None and linked_item is None:
            return

        target_case_id = case_id or str(event.get_extra("ai_inbox_case_id") or "")
        targets = [
            candidate for candidate in (linked_item, item) if candidate is not None
        ]
        seen: set[str] = set()
        try:
            for target in targets:
                if target.item_id in seen:
                    continue
                seen.add(target.item_id)
                await self.store.update_item(
                    target.item_id,
                    status=status,  # type: ignore[arg-type]
                    case_id=target_case_id or target.case_id,
                    task_id=task_id,
                    event_type="task_linked",
                    event_payload={"source": source},
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[ai_inbox] link task 失败：%s", exc)

    def _should_track(self, event: AstrMessageEvent, text: str) -> bool:
        if not text.strip():
            return False
        normalized = text.strip()
        if normalized.startswith(_COMMANDS_TO_IGNORE):
            return False
        try:
            if str(event.get_sender_id() or "") == str(event.get_self_id() or ""):
                return False
        except Exception:  # noqa: BLE001
            pass

        platform_id = event.get_platform_id() or ""
        platform_name = (event.get_platform_name() or "").lower()
        if (
            platform_id not in _TRACKED_PLATFORM_IDS
            and platform_name != "lark"
            and not str(event.unified_msg_origin or "").lower().startswith("lark:")
        ):
            return False

        if self._is_group_event(event):
            return bool(getattr(event, "is_at_or_wake_command", False))
        return True

    async def _maybe_record_obsidian_review(
        self,
        event: AstrMessageEvent,
        item_id: str,
        text: str,
    ) -> None:
        if not looks_like_obsidian_review_reply(text):
            return
        get_extra = getattr(event, "get_extra", None)
        memory_context = (
            get_extra("dc_agent_memory_context") if callable(get_extra) else None
        )
        record = build_review_record(
            text=text,
            sender_id=str(event.get_sender_id() or ""),
            sender_name=event.get_sender_name() or str(event.get_sender_id() or ""),
            session_id=event.unified_msg_origin or "",
            platform_id=event.get_platform_id() or "",
            memory_context=memory_context if isinstance(memory_context, dict) else None,
        )
        try:
            append_review_record(record)
            if self.store is not None:
                await self.store.update_item(
                    item_id,
                    payload_patch={
                        "obsidian_review_id": record.review_id,
                        "obsidian_review_action": record.action,
                        "obsidian_review_candidates": [
                            candidate.doc_key or candidate.title
                            for candidate in record.candidates
                        ],
                    },
                    event_type="obsidian_review_recorded",
                    event_payload={
                        "review_id": record.review_id,
                        "action": record.action,
                    },
                )
            logger.info(
                "[ai_inbox] Obsidian 复核记录已保存 review=%s action=%s candidates=%s",
                record.review_id[:8],
                record.action,
                len(record.candidates),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[ai_inbox] Obsidian 复核记录保存失败：%s", exc)

    def _is_group_event(self, event: AstrMessageEvent) -> bool:
        is_group = getattr(event, "is_group", None)
        if callable(is_group):
            try:
                return bool(is_group())
            except Exception:  # noqa: BLE001
                pass
        try:
            if event.get_group_id():
                return True
        except Exception:  # noqa: BLE001
            pass
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        return "GroupMessage" in umo or ":group:" in umo.lower()

    async def _conversation_id(self, event: AstrMessageEvent) -> str:
        manager = getattr(self.context, "conversation_manager", None)
        if manager is None:
            return ""
        try:
            conversation_id = await manager.get_curr_conversation_id(
                event.unified_msg_origin
            )
            if conversation_id:
                return str(conversation_id)
            return str(
                await manager.new_conversation(
                    event.unified_msg_origin,
                    event.get_platform_id(),
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[ai_inbox] 获取 conversation_id 失败：%s", exc)
            return ""

    def _extract_text(self, event: AstrMessageEvent) -> str:
        parts = [
            str(component.text)
            for component in event.get_messages()
            if isinstance(component, Plain)
        ]
        return ("".join(parts) or event.message_str or "").strip()

    def _case_name_seed(self, sender_name: str, text: str) -> str:
        sender = sender_name.strip() or "同事"
        brief = " ".join(text.strip().split())[:36]
        if brief:
            return f"协作收件箱 · {sender} · {brief}"
        return f"协作收件箱 · {sender}"

    async def _api_items(self, *args, **kwargs):
        if self.store is None:
            return {"status": "error", "message": "InboxStore not ready", "data": None}
        try:
            items = await self.store.list_items(limit=100)
            return {
                "status": "ok",
                "message": None,
                "data": {
                    "items": [self._item_to_dict(item) for item in items],
                    "total": len(items),
                },
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("[ai_inbox] _api_items 异常：%s", exc)
            return {"status": "error", "message": str(exc), "data": None}

    async def _api_stats(self, *args, **kwargs):
        if self.store is None:
            return {"status": "error", "message": "InboxStore not ready", "data": None}
        try:
            stats = await self.store.stats()
            return {"status": "ok", "message": None, "data": stats}
        except Exception as exc:  # noqa: BLE001
            logger.warning("[ai_inbox] _api_stats 异常：%s", exc)
            return {"status": "error", "message": str(exc), "data": None}

    def _item_to_dict(self, item: InboxItem) -> dict[str, Any]:
        return {
            "item_id": item.item_id,
            "session_id": item.session_id,
            "conversation_id": item.conversation_id,
            "platform_id": item.platform_id,
            "sender_id": item.sender_id,
            "sender_name": item.sender_name,
            "text": item.text,
            "category": item.category,
            "status": item.status,
            "case_id": item.case_id,
            "task_id": item.task_id,
            "source": item.source,
            "payload": item.payload,
            "created_at": item.created_at,
            "updated_at": item.updated_at,
        }
