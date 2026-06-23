"""Feishu work pet assistant — MVP P0 路由层。

设计：
- 飞书事件通过现有 DC-Agent/AstrBot 长连接进入，不另搭 webhook。
- 这里只做命令路由 + 卡片发送，业务规则在 service.py，SQL 在 store.py，
  卡片在 cards.py。
- user_id 统一用飞书 sender open_id。普通消息走 event.get_sender_id()，
  卡片回调由 lark_adapter 把 abm.session_id / abm.sender.user_id 也设成 open_id，
  所以同一份取法。
"""

from __future__ import annotations

import json
import os
from typing import Any

from dc_engines.card_runtime import send_card_via_runtime
from dc_engines.feishu_card_streamer import (
    ensure_streamers_on_context,
    extract_chat_info_from_event,
)
from dc_engines.pet_live.contracts import PetSourceRef, StoredPetEvent
from dc_engines.pet_live.integrations import employee_id_from_source, pet_live_enabled
from dc_engines.pet_live.service import PetLiveService
from dc_engines.pet_live.store import PetLiveStore

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.star import Context, Star, register

from . import cards
from .service import PetService
from .store import PetStore

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
# data/plugins/feishu_pet_assistant/ → data/feishu_pet.db
DEFAULT_DB_PATH = os.path.normpath(
    os.path.join(PLUGIN_DIR, "..", "..", "feishu_pet.db")
)


@register(
    "feishu_pet_assistant",
    "dc_agent",
    "飞书工作宠物助手 MVP：/pet 状态卡 + 任务闭环 + SQLite 持久化",
    "0.2.0",
)
class FeishuPetAssistantPlugin(Star):
    def __init__(self, context: Context, config: dict[str, Any] | None = None) -> None:
        super().__init__(context)
        conf = config or {}
        db_path = conf.get("db_path") or DEFAULT_DB_PATH
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._store = PetStore(db_path)
        self._service = PetService(self._store)
        self._live_service = PetLiveService(PetLiveStore(db_path))
        self._h5_url: str | None = conf.get("h5_url") or None
        self._desktop_entry_url: str | None = (
            conf.get("desktop_entry_url")
            or os.environ.get("PET_LIVE_DESKTOP_ENTRY_URL")
            or None
        )
        logger.info(
            "[FeishuPet] 初始化完成 db=%s h5_url=%s desktop_entry_url=%s",
            db_path,
            self._h5_url or "(未配置)",
            self._desktop_entry_url or "(未配置)",
        )

    def _get_formatted_h5_url(self, user_id: str) -> str | None:
        return self._format_url_template(self._h5_url, user_id=user_id)

    def _get_formatted_desktop_url(
        self,
        user_id: str,
        live_state: dict[str, Any] | None,
    ) -> str | None:
        return self._format_url_template(
            self._desktop_entry_url,
            user_id=user_id,
            pet_id=str((live_state or {}).get("pet_id") or ""),
        )

    @staticmethod
    def _format_url_template(
        template: str | None,
        *,
        user_id: str,
        pet_id: str = "",
    ) -> str | None:
        if not template:
            return None
        url = template
        if "{user_id}" in url:
            url = url.replace("{user_id}", user_id)
        if "${user_id}" in url:
            url = url.replace("${user_id}", user_id)
        if "{pet_id}" in url:
            url = url.replace("{pet_id}", pet_id)
        if "${pet_id}" in url:
            url = url.replace("${pet_id}", pet_id)
        if user_id not in url:
            separator = "&" if "?" in url else "?"
            return f"{url}{separator}user_id={user_id}"
        return url

    # ── 命令路由 ──────────────────────────────────────────────────────────

    @filter.regex(r"^/?pet\s*$")
    async def pet_status(self, event: AstrMessageEvent) -> None:
        """/pet → 返回宠物状态卡。"""
        user_id = self._user_id(event)
        if not user_id:
            return
        pet = self._service.get_or_create_pet(user_id)
        live_event = self._record_live_event(
            event,
            user_id=user_id,
            event_type="pet_card_viewed",
            payload={"command": "/pet"},
        )
        stats = self._service.build_stats(user_id)
        live_state = self._live_state_payload(live_event)
        desktop_bound = self._desktop_bound_for_user(user_id)
        card = cards.build_status_card(
            pet,
            stats,
            h5_url=self._get_formatted_h5_url(user_id),
            live_state=live_state,
            desktop_url=self._get_formatted_desktop_url(user_id, live_state),
            desktop_bound=desktop_bound,
        )
        if await self._send_card(event, card, card_type="pet_status"):
            event.stop_event()
            return
        self._reply(
            event,
            cards.render_status_text(
                pet,
                stats,
                live_state=live_state,
                desktop_bound=desktop_bound,
            ),
        )

    @filter.regex(r"^(看看任务|/tasks?)\s*$")
    async def task_list(self, event: AstrMessageEvent) -> None:
        """看看任务 / /tasks → 任务卡。"""
        user_id = self._user_id(event)
        if not user_id:
            return
        pet = self._service.get_or_create_pet(user_id)
        tasks = self._service.list_today_tasks(user_id)
        live_state = self._live_state_for_user(user_id)
        card = cards.build_tasks_card(
            pet,
            tasks,
            h5_url=self._get_formatted_h5_url(user_id),
            desktop_url=self._get_formatted_desktop_url(user_id, live_state),
        )
        if await self._send_card(event, card, card_type="pet_tasks"):
            event.stop_event()
            return
        self._reply(event, cards.render_tasks_text(tasks))

    @filter.regex(r"^/done\s+1\s*$")
    async def complete_first_task(self, event: AstrMessageEvent) -> None:
        """/done 1 → 完成第一条 pending 任务。"""
        user_id = self._user_id(event)
        if not user_id:
            return
        self._service.get_or_create_pet(user_id)
        result = self._service.complete_first_pending(user_id)
        if result is None:
            self._reply(event, cards.NO_REAL_TASKS_TEXT)
            return
        pet, task = result
        live_event = self._record_live_event(
            event,
            user_id=user_id,
            event_type="task_completed",
            payload={"task_id": task["id"], "task_source": task.get("source", "")},
        )
        stats = self._service.build_stats(user_id)
        live_state = self._live_state_payload(live_event)
        card = cards.build_done_card(
            pet,
            task,
            stats,
            h5_url=self._get_formatted_h5_url(user_id),
            live_state=live_state,
            desktop_url=self._get_formatted_desktop_url(user_id, live_state),
        )
        if await self._send_card(event, card, card_type="pet_done"):
            event.stop_event()
            return
        self._reply(
            event, cards.render_done_text(pet, task, stats, live_state=live_state)
        )

    @filter.regex(r"^__card_action__:")
    async def handle_card_action(self, event: AstrMessageEvent) -> None:
        """飞书卡片按钮回调（lark_adapter 转 __card_action__: 伪消息）。"""
        user_id = self._user_id(event)
        if not user_id:
            return

        payload = self._parse_card_action(event)
        # Robust extraction: some card actions put fields at top level of payload, others wrap in "value".
        # Support both the inner value and top-level for source/action (defensive against parse variations).
        if isinstance(payload, dict):
            inner_value = payload.get("value", {}) or {}
            if isinstance(inner_value, dict) and inner_value:
                value = inner_value
            else:
                value = payload
        else:
            value = {}
        action = value.get("action") if isinstance(value, dict) else None
        source = value.get("source") if isinstance(value, dict) else None
        logger.info(
            "[FeishuPet] handle_card_action user=%s action=%s source=%s",
            user_id[:12] if user_id else "",
            action,
            source,
        )
        if action:
            self._record_live_event(
                event,
                user_id=user_id,
                event_type="pet_card_action",
                payload={"action": action, "source": source or ""},
            )

        # ── 隔离膜：不处理其他插件渲染的卡片来源 ──────────────────────────
        if source == "department_memory_prompt":
            logger.info(
                "[FeishuPet] 透传 department_memory_prompt card_action，停止本插件传播让 dc_router 接管。"
            )
            event.stop_event()
            return
        if source is not None and source not in {
            "pet_system",  # pet 自己的卡片（如有）
            "daily_response",
            "thinking_waiting",
            "casual_reply",
        }:
            logger.info(
                "[FeishuPet] 透传未知 source=%s action=%s，停止本插件传播。",
                source,
                action,
            )
            event.stop_event()
            return
        # ── 以下是 pet 自己的按钮处理 ──────────────────────────────────────
        if action == "pet_view_tasks":
            pet = self._service.get_or_create_pet(user_id)
            tasks = self._service.list_today_tasks(user_id)
            live_state = self._live_state_for_user(user_id)
            card = cards.build_tasks_card(
                pet,
                tasks,
                h5_url=self._get_formatted_h5_url(user_id),
                desktop_url=self._get_formatted_desktop_url(user_id, live_state),
            )
            if await self._send_card(event, card, card_type="pet_tasks"):
                event.stop_event()
            return

        if action == "pet_done_first":
            self._service.get_or_create_pet(user_id)
            result = self._service.complete_first_pending(user_id)
            if result is None:
                await self._send_card(
                    event,
                    cards.build_error_card(cards.NO_REAL_TASKS_TEXT),
                    card_type="pet_error",
                )
                event.stop_event()
                return
            pet, task = result
            live_event = self._record_live_event(
                event,
                user_id=user_id,
                event_type="task_completed",
                payload={"task_id": task["id"], "task_source": task.get("source", "")},
            )
            stats = self._service.build_stats(user_id)
            live_state = self._live_state_payload(live_event)
            card = cards.build_done_card(
                pet,
                task,
                stats,
                h5_url=self._get_formatted_h5_url(user_id),
                live_state=live_state,
                desktop_url=self._get_formatted_desktop_url(user_id, live_state),
            )
            if await self._send_card(event, card, card_type="pet_done"):
                event.stop_event()
            return

        if action == "pet_done_task":
            task_id = value.get("task_id") if isinstance(value, dict) else None
            if not isinstance(task_id, str) or not task_id:
                await self._send_card(
                    event,
                    cards.build_error_card("按钮没带 task_id，刷一下卡片再点。"),
                    card_type="pet_error",
                )
                event.stop_event()
                return
            self._service.get_or_create_pet(user_id)
            result = self._service.complete_task(user_id, task_id)
            if result is None:
                await self._send_card(
                    event,
                    cards.build_error_card("这条任务找不到或不属于你。"),
                    card_type="pet_error",
                )
                event.stop_event()
                return
            pet, task = result
            live_event = self._record_live_event(
                event,
                user_id=user_id,
                event_type="task_completed",
                payload={"task_id": task["id"], "task_source": task.get("source", "")},
            )
            stats = self._service.build_stats(user_id)
            live_state = self._live_state_payload(live_event)
            card = cards.build_done_card(
                pet,
                task,
                stats,
                h5_url=self._get_formatted_h5_url(user_id),
                live_state=live_state,
                desktop_url=self._get_formatted_desktop_url(user_id, live_state),
            )
            if await self._send_card(event, card, card_type="pet_done"):
                event.stop_event()
            return

        logger.info("[FeishuPet] 未识别的 card_action value=%s", value)

    # ── 工具方法 ──────────────────────────────────────────────────────────

    @staticmethod
    def _user_id(event: AstrMessageEvent) -> str:
        """统一用飞书 sender open_id 当 user_id。卡片回调和普通消息都用同一个。"""
        sender_id = event.get_sender_id() or ""
        if not sender_id:
            logger.warning("[FeishuPet] 拿不到 sender_id，跳过事件")
        return sender_id

    def _record_live_event(
        self,
        event: AstrMessageEvent,
        *,
        user_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> StoredPetEvent | None:
        """Best-effort bridge into Pet Live Core.

        Pet live telemetry must never break the Feishu assistant's existing card
        and text fallback behavior, so failures are logged and swallowed.
        """

        if not pet_live_enabled():
            return None

        try:
            employee_id = employee_id_from_source(event)
            identity = self._live_service.get_or_create_identity(
                feishu_open_id=user_id,
                employee_id=employee_id,
            )
            return self._live_service.record_event(
                pet_id=identity.pet_id,
                user_id=identity.employee_id or identity.feishu_open_id,
                source="feishu",
                event_type=event_type,
                source_ref=self._source_ref_for_event(event).to_dict(),
                payload={"employee_id": identity.employee_id, **(payload or {})},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[FeishuPet] live event 记录失败 user=%s event=%s err=%s",
                user_id[:12],
                event_type,
                exc,
            )
            return None

    @staticmethod
    def _live_state_payload(event: StoredPetEvent | None) -> dict[str, Any] | None:
        if event is None or event.state_after is None:
            return None
        return event.state_after.to_dict()

    def _live_state_for_user(self, user_id: str) -> dict[str, Any] | None:
        if not pet_live_enabled():
            return None
        try:
            identity = self._live_service.store.get_identity_by_employee_id(
                user_id
            ) or self._live_service.store.get_identity_by_feishu_open_id(user_id)
            if identity is None:
                return None
            state = self._live_service.get_pet_state(identity.pet_id)
            return state.to_dict() if state is not None else None
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[FeishuPet] live state 读取失败 user=%s err=%s",
                user_id[:12],
                exc,
            )
            return None

    def _desktop_bound_for_user(self, user_id: str) -> bool | None:
        if not pet_live_enabled():
            return None
        try:
            identity = self._live_service.store.get_identity_by_employee_id(
                user_id
            ) or self._live_service.store.get_identity_by_feishu_open_id(user_id)
            if identity is None:
                return False
            return bool(identity.desktop_session_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[FeishuPet] desktop binding 状态读取失败 user=%s err=%s",
                user_id[:12],
                exc,
            )
            return None

    @staticmethod
    def _source_ref_for_event(event: AstrMessageEvent) -> PetSourceRef:
        payload = getattr(
            getattr(event, "message_obj", None), "card_action_payload", None
        )
        message_id = str(getattr(event, "message_id", "") or "")
        if not message_id:
            message_id = str(
                getattr(getattr(event, "message_obj", None), "message_id", "") or ""
            )
        chat_id = ""
        if isinstance(payload, dict):
            chat_id = str(payload.get("open_chat_id") or "")
        conversation_id = (
            chat_id
            or str(getattr(event, "session_id", "") or "")
            or str(getattr(event, "conversation_id", "") or "")
        )
        return PetSourceRef(
            employee_id=employee_id_from_source(event),
            platform=str(getattr(event, "get_platform_name", lambda: "")() or ""),
            conversation_id=conversation_id,
            message_id=message_id,
        )

    async def _send_card(
        self,
        event: AstrMessageEvent,
        card: dict,
        *,
        card_type: str = "pet_status",
    ) -> bool:
        if (event.get_platform_name() or "").lower() != "lark":
            return False

        streamer = ensure_streamers_on_context(self.context).get(
            event.get_platform_id() or ""
        )
        if streamer is None:
            return False

        payload = getattr(event.message_obj, "card_action_payload", None) or {}
        chat_id = payload.get("open_chat_id") if isinstance(payload, dict) else ""
        if chat_id:
            receive_id_type = "chat_id"
        else:
            chat_id, receive_id_type = extract_chat_info_from_event(event)
        if not chat_id:
            return False

        stream = await send_card_via_runtime(
            streamer,
            card_type=card_type,
            chat_id=chat_id,
            receive_id_type=receive_id_type,
            card=card,
            platform_id=event.get_platform_id() or "",
            event="start",
            detail="feishu pet assistant card",
        )
        return stream is not None

    def _reply(self, event: AstrMessageEvent, text: str) -> None:
        event.set_result(MessageEventResult().message(text).use_t2i(False).stop_event())

    def _parse_card_action(self, event: AstrMessageEvent) -> dict:
        payload = getattr(event.message_obj, "card_action_payload", None)
        if isinstance(payload, dict):
            return payload

        text = (event.message_str or "").strip()
        if not text.startswith("__card_action__:"):
            return {}
        try:
            parsed = json.loads(text[len("__card_action__:") :])
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
