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
        self._h5_url: str | None = conf.get("h5_url") or None
        logger.info(
            "[FeishuPet] 初始化完成 db=%s h5_url=%s",
            db_path,
            self._h5_url or "(未配置)",
        )

    def _get_formatted_h5_url(self, user_id: str) -> str | None:
        if not self._h5_url:
            return None
        url = self._h5_url
        if "{user_id}" in url:
            return url.replace("{user_id}", user_id)
        if "${user_id}" in url:
            return url.replace("${user_id}", user_id)
        separator = "&" if "?" in url else "?"
        return f"{url}{separator}user_id={user_id}"

    # ── 命令路由 ──────────────────────────────────────────────────────────

    @filter.regex(r"^/?pet\s*$")
    async def pet_status(self, event: AstrMessageEvent) -> None:
        """/pet → 返回宠物状态卡。"""
        user_id = self._user_id(event)
        if not user_id:
            return
        pet = self._service.get_or_create_pet(user_id)
        stats = self._service.build_stats(user_id)
        card = cards.build_status_card(
            pet, stats, h5_url=self._get_formatted_h5_url(user_id)
        )
        if await self._send_card(event, card):
            event.stop_event()
            return
        self._reply(event, cards.render_status_text(pet, stats))

    @filter.regex(r"^(看看任务|/tasks?)\s*$")
    async def task_list(self, event: AstrMessageEvent) -> None:
        """看看任务 / /tasks → 任务卡。"""
        user_id = self._user_id(event)
        if not user_id:
            return
        pet = self._service.get_or_create_pet(user_id)
        tasks = self._service.list_today_tasks(user_id)
        card = cards.build_tasks_card(
            pet, tasks, h5_url=self._get_formatted_h5_url(user_id)
        )
        if await self._send_card(event, card):
            event.stop_event()
            return
        self._reply(event, cards.render_tasks_text(tasks))

    @filter.regex(r"^/done\s+1\s*$")
    async def complete_first_task(self, event: AstrMessageEvent) -> None:
        """/done 1 → 完成第一条 pending 任务。"""
        user_id = self._user_id(event)
        if not user_id:
            return
        self._service.get_or_create_pet(user_id)  # 确保 demo 任务已种
        result = self._service.complete_first_pending(user_id)
        if result is None:
            self._reply(event, "今天没有待办了，小橘可以躺着晒太阳 🌞")
            return
        pet, task = result
        stats = self._service.build_stats(user_id)
        card = cards.build_done_card(
            pet, task, stats, h5_url=self._get_formatted_h5_url(user_id)
        )
        if await self._send_card(event, card):
            event.stop_event()
            return
        self._reply(event, cards.render_done_text(pet, task, stats))

    @filter.regex(r"^__card_action__:")
    async def handle_card_action(self, event: AstrMessageEvent) -> None:
        """飞书卡片按钮回调（lark_adapter 转 __card_action__: 伪消息）。"""
        user_id = self._user_id(event)
        if not user_id:
            return

        payload = self._parse_card_action(event)
        value = payload.get("value", {}) if isinstance(payload, dict) else {}
        action = value.get("action") if isinstance(value, dict) else None
        logger.info(
            "[FeishuPet] handle_card_action user=%s action=%s",
            user_id[:12],
            action,
        )

        if action == "pet_view_tasks":
            pet = self._service.get_or_create_pet(user_id)
            tasks = self._service.list_today_tasks(user_id)
            card = cards.build_tasks_card(
                pet, tasks, h5_url=self._get_formatted_h5_url(user_id)
            )
            if await self._send_card(event, card):
                event.stop_event()
            return

        if action == "pet_done_first":
            self._service.get_or_create_pet(user_id)
            result = self._service.complete_first_pending(user_id)
            if result is None:
                await self._send_card(
                    event, cards.build_error_card("今天没有待办了，小橘可以躺着 🌞")
                )
                event.stop_event()
                return
            pet, task = result
            stats = self._service.build_stats(user_id)
            card = cards.build_done_card(
                pet, task, stats, h5_url=self._get_formatted_h5_url(user_id)
            )
            if await self._send_card(event, card):
                event.stop_event()
            return

        if action == "pet_done_task":
            task_id = value.get("task_id") if isinstance(value, dict) else None
            if not isinstance(task_id, str) or not task_id:
                await self._send_card(
                    event, cards.build_error_card("按钮没带 task_id，刷一下卡片再点。")
                )
                event.stop_event()
                return
            self._service.get_or_create_pet(user_id)
            result = self._service.complete_task(user_id, task_id)
            if result is None:
                await self._send_card(
                    event, cards.build_error_card("这条任务找不到或不属于你。")
                )
                event.stop_event()
                return
            pet, task = result
            stats = self._service.build_stats(user_id)
            card = cards.build_done_card(
                pet, task, stats, h5_url=self._get_formatted_h5_url(user_id)
            )
            if await self._send_card(event, card):
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

    async def _send_card(self, event: AstrMessageEvent, card: dict) -> bool:
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
            card_type="daily_response",
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
