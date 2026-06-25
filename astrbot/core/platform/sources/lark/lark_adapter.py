import asyncio
import base64
import json
import re
import time
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    GetMessageRequest,
    GetMessageResourceRequest,
    ListMessageRequest,
)
from lark_oapi.api.im.v1.processor import (
    P2ImChatAccessEventBotP2pChatEnteredV1Processor,
    P2ImMessageReceiveV1Processor,
)

import astrbot.api.message_components as Comp
from astrbot import logger
from astrbot.api.event import MessageChain
from astrbot.api.platform import (
    AstrBotMessage,
    MessageMember,
    MessageType,
    Platform,
    PlatformMetadata,
)
from astrbot.core.platform.astr_message_event import MessageSesion
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path
from astrbot.core.utils.webhook_utils import log_webhook_info

from ...register import register_platform_adapter
from .bot_info import request_lark_bot_info
from .lark_event import LarkMessageEvent
from .server import LarkWebhookServer


@register_platform_adapter(
    "lark", "飞书机器人官方 API 适配器", support_streaming_message=True
)
class LarkPlatformAdapter(Platform):
    MESSAGE_ID_DEDUPE_TTL_SECONDS = 1800

    def __init__(
        self,
        platform_config: dict,
        platform_settings: dict,
        event_queue: asyncio.Queue,
    ) -> None:
        super().__init__(platform_config, event_queue)

        self.appid = platform_config["app_id"]
        self.appsecret = platform_config["app_secret"]
        self.domain = platform_config.get("domain", lark.FEISHU_DOMAIN)
        self.bot_name = "astrbot"
        self.bot_open_id = ""

        # socket or webhook
        self.connection_mode = platform_config.get("lark_connection_mode", "socket")

        # 初始化 WebSocket 长连接相关配置
        async def on_msg_event_recv(event: lark.im.v1.P2ImMessageReceiveV1) -> None:
            await self.convert_msg(event)

        def do_v2_msg_event(event: lark.im.v1.P2ImMessageReceiveV1) -> None:
            asyncio.create_task(on_msg_event_recv(event))

        # ─── 卡片按钮回调：转成 AstrBotMessage 投到事件队列 ───
        # 插件用 message_str 前缀 "__card_action__:" 识别（JSON payload）
        async def on_card_action_recv(event) -> None:
            await self.convert_card_action(event)

        def do_card_action_event(event) -> None:
            asyncio.create_task(on_card_action_recv(event))

        async def on_p2p_chat_entered_recv(event) -> None:
            await self.convert_p2p_chat_entered(event)

        def do_p2p_chat_entered_event(event) -> None:
            asyncio.create_task(on_p2p_chat_entered_recv(event))

        self.event_handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(do_v2_msg_event)
            .register_p2_card_action_trigger(do_card_action_event)
            .register_p2_im_chat_access_event_bot_p2p_chat_entered_v1(
                do_p2p_chat_entered_event
            )
            .build()
        )

        self.do_v2_msg_event = do_v2_msg_event
        self.do_p2p_chat_entered_event = do_p2p_chat_entered_event

        self.client = lark.ws.Client(
            app_id=self.appid,
            app_secret=self.appsecret,
            log_level=lark.LogLevel.ERROR,
            domain=self.domain,
            event_handler=self.event_handler,
        )
        self._socket_connected_at: float | None = None
        self._socket_reconnect_attempts = 0
        self._last_event_at: float | None = None
        self._install_socket_observers()

        self.lark_api = (
            lark.Client.builder()
            .app_id(self.appid)
            .app_secret(self.appsecret)
            .log_level(lark.LogLevel.ERROR)
            .domain(self.domain)
            .build()
        )

        self.webhook_server = None
        if self.connection_mode == "webhook":
            self.webhook_server = LarkWebhookServer(platform_config, event_queue)
            self.webhook_server.set_callback(self.handle_webhook_event)

        self.event_id_timestamps: dict[str, float] = {}
        self.message_id_timestamps: dict[str, float] = {}
        self.message_id_store_path = self._default_message_id_store_path()
        self._message_id_store_loaded = False
        self._load_persisted_message_ids()
        self.polling_fallback_enabled = bool(
            platform_config.get("lark_polling_fallback_enabled", False)
        )
        polling_chat_ids = platform_config.get("lark_polling_fallback_chat_ids", [])
        self.polling_fallback_chat_ids = [
            str(chat_id).strip() for chat_id in polling_chat_ids if str(chat_id).strip()
        ]
        self.polling_chat_store_path = self._default_polling_chat_store_path()
        self._load_persisted_polling_chat_ids()
        if self.polling_fallback_enabled and self.polling_fallback_chat_ids:
            self._persist_polling_chat_ids()
        self.polling_fallback_interval = max(
            3,
            int(platform_config.get("lark_polling_fallback_interval_sec", 8) or 8),
        )
        self.polling_fallback_backfill_seconds = max(
            60,
            int(platform_config.get("lark_polling_fallback_backfill_minutes", 10) or 10)
            * 60,
        )
        self.polling_fallback_page_size = min(
            50,
            max(
                5, int(platform_config.get("lark_polling_fallback_page_size", 20) or 20)
            ),
        )
        self._polling_fallback_task: asyncio.Task | None = None
        self._polling_next_start_time = self._initial_polling_start_time(
            has_persisted_seen_messages=bool(self.message_id_timestamps)
        )
        self._polling_next_start_times: dict[str, int] = dict.fromkeys(
            self.polling_fallback_chat_ids,
            self._polling_next_start_time,
        )
        self.multimodal_merge_window_seconds = max(
            0.0,
            float(
                platform_config.get(
                    "lark_multimodal_merge_window_seconds",
                    platform_config.get("lark_image_text_merge_window_seconds", 5),
                )
                or 5
            ),
        )
        self.file_multimodal_merge_window_seconds = max(
            self.multimodal_merge_window_seconds,
            float(platform_config.get("lark_file_text_merge_window_seconds", 60) or 60),
        )
        self._pending_multimodal_messages: dict[
            str, tuple[AstrBotMessage, asyncio.Task, float]
        ] = {}

    def _install_socket_observers(self) -> None:
        if self.connection_mode == "webhook":
            return

        previous_reconnecting = getattr(self.client, "on_reconnecting", None)
        previous_reconnected = getattr(self.client, "on_reconnected", None)

        def on_reconnecting() -> None:
            self._socket_reconnect_attempts += 1
            logger.warning(
                "[Lark.Socket] reconnecting platform=%s attempts=%s last_event_age=%.1fs",
                self.meta().id,
                self._socket_reconnect_attempts,
                self._seconds_since(self._last_event_at),
            )
            if callable(previous_reconnecting):
                previous_reconnecting()

        def on_reconnected() -> None:
            self._mark_socket_connected(source="reconnect")
            if callable(previous_reconnected):
                previous_reconnected()

        if hasattr(self.client, "on_reconnecting"):
            self.client.on_reconnecting = on_reconnecting
        if hasattr(self.client, "on_reconnected"):
            self.client.on_reconnected = on_reconnected

    @staticmethod
    def _seconds_since(ts: float | None) -> float:
        if ts is None:
            return -1.0
        return max(0.0, time.time() - ts)

    def _mark_socket_connected(self, *, source: str) -> None:
        self._socket_connected_at = time.time()
        conn_id = getattr(self.client, "_conn_id", "") or ""
        service_id = getattr(self.client, "_service_id", "") or ""
        logger.info(
            "[Lark.Socket] connected platform=%s source=%s conn_id=%s service_id=%s",
            self.meta().id,
            source,
            str(conn_id)[:12],
            str(service_id)[:12],
        )

    async def _download_message_resource(
        self,
        *,
        message_id: str,
        file_key: str,
        resource_type: str,
    ) -> bytes | None:
        if self.lark_api.im is None:
            logger.error("[Lark] API Client im 模块未初始化")
            return None

        request = (
            GetMessageResourceRequest.builder()
            .message_id(message_id)
            .file_key(file_key)
            .type(resource_type)
            .build()
        )
        response = await self.lark_api.im.v1.message_resource.aget(request)
        if not response.success():
            logger.error(
                f"[Lark] 下载消息资源失败 type={resource_type}, key={file_key}, "
                f"code={response.code}, msg={response.msg}",
            )
            return None

        if response.file is None:
            logger.error(f"[Lark] 消息资源响应中不包含文件流: {file_key}")
            return None

        return response.file.read()

    @staticmethod
    def _build_message_str_from_components(
        components: list[Comp.BaseMessageComponent],
    ) -> str:
        parts: list[str] = []
        for comp in components:
            if isinstance(comp, Comp.Plain):
                text = comp.text.strip()
                if text:
                    parts.append(text)
            elif isinstance(comp, Comp.At):
                name = str(comp.name or comp.qq or "").strip()
                if name:
                    parts.append(f"@{name}")
            elif isinstance(comp, Comp.Image):
                parts.append("[image]")
            elif isinstance(comp, Comp.File):
                parts.append(str(comp.name or "[file]"))
            elif isinstance(comp, Comp.Record):
                parts.append("[audio]")
            elif isinstance(comp, Comp.Video):
                parts.append("[video]")

        return " ".join(parts).strip()

    @staticmethod
    def _parse_post_content(content: dict[str, Any]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for item in content.get("content", []):
            if isinstance(item, list):
                for comp in item:
                    if isinstance(comp, dict):
                        result.append(comp)
            elif isinstance(item, dict):
                result.append(item)
        return result

    @staticmethod
    def _build_at_map(mentions: list[Any] | None) -> dict[str, Comp.At]:
        at_map: dict[str, Comp.At] = {}
        if not mentions:
            return at_map

        for mention in mentions:
            key = getattr(mention, "key", None)
            if not key:
                continue

            mention_id = getattr(mention, "id", None)
            open_id = ""
            if mention_id is not None:
                if hasattr(mention_id, "open_id"):
                    open_id = getattr(mention_id, "open_id", "") or ""
                else:
                    open_id = str(mention_id)

            mention_name = str(getattr(mention, "name", "") or "")
            at_map[key] = Comp.At(qq=open_id, name=mention_name)

        return at_map

    async def _parse_message_components(
        self,
        *,
        message_id: str | None,
        message_type: str,
        content: dict[str, Any],
        at_map: dict[str, Comp.At],
    ) -> list[Comp.BaseMessageComponent]:
        components: list[Comp.BaseMessageComponent] = []

        if message_type == "text":
            message_str_raw = str(content.get("text", ""))
            at_pattern = r"(@_user_\d+)"
            parts = re.split(at_pattern, message_str_raw)
            for part in parts:
                segment = part.strip()
                if not segment:
                    continue
                if segment in at_map:
                    components.append(at_map[segment])
                else:
                    components.append(Comp.Plain(segment))
            return components

        if message_type in ("post", "image"):
            if message_type == "image":
                comp_list = [
                    {
                        "tag": "img",
                        "image_key": content.get("image_key"),
                    },
                ]
            else:
                comp_list = self._parse_post_content(content)

            for comp in comp_list:
                tag = comp.get("tag")
                if tag == "at":
                    user_key = str(comp.get("user_id", ""))
                    if user_key in at_map:
                        components.append(at_map[user_key])
                elif tag == "text":
                    text = str(comp.get("text", "")).strip()
                    if text:
                        components.append(Comp.Plain(text))
                elif tag == "a":
                    text = str(comp.get("text", "")).strip()
                    href = str(comp.get("href", "")).strip()
                    if text and href:
                        components.append(Comp.Plain(f"{text}({href})"))
                    elif text:
                        components.append(Comp.Plain(text))
                elif tag == "img":
                    image_key = str(comp.get("image_key", "")).strip()
                    if not image_key:
                        continue
                    if not message_id:
                        logger.error("[Lark] 图片消息缺少 message_id")
                        continue
                    image_bytes = await self._download_message_resource(
                        message_id=message_id,
                        file_key=image_key,
                        resource_type="image",
                    )
                    if image_bytes is None:
                        continue
                    image_base64 = base64.b64encode(image_bytes).decode()
                    components.append(Comp.Image.fromBase64(image_base64))
                elif tag == "media":
                    file_key = str(comp.get("file_key", "")).strip()
                    file_name = (
                        str(comp.get("file_name", "")).strip() or "lark_media.mp4"
                    )
                    if not file_key:
                        continue
                    if not message_id:
                        logger.error("[Lark] 富文本视频消息缺少 message_id")
                        continue
                    file_path = await self._download_file_resource_to_temp(
                        message_id=message_id,
                        file_key=file_key,
                        message_type="post_media",
                        file_name=file_name,
                        default_suffix=".mp4",
                    )
                    if file_path:
                        components.append(Comp.Video(file=file_path, path=file_path))

            return components

        if message_type == "file":
            file_key = str(content.get("file_key", "")).strip()
            file_name = str(content.get("file_name", "")).strip() or "lark_file"
            if not message_id:
                logger.error("[Lark] 文件消息缺少 message_id")
                return components
            if not file_key:
                logger.error("[Lark] 文件消息缺少 file_key")
                return components
            file_path = await self._download_file_resource_to_temp(
                message_id=message_id,
                file_key=file_key,
                message_type="file",
                file_name=file_name,
            )
            if file_path:
                components.append(Comp.File(name=file_name, file=file_path))
            return components

        if message_type == "audio":
            file_key = str(content.get("file_key", "")).strip()
            if not message_id:
                logger.error("[Lark] 音频消息缺少 message_id")
                return components
            if not file_key:
                logger.error("[Lark] 音频消息缺少 file_key")
                return components
            file_path = await self._download_file_resource_to_temp(
                message_id=message_id,
                file_key=file_key,
                message_type="audio",
                default_suffix=".opus",
            )
            if file_path:
                components.append(Comp.Record(file=file_path, url=file_path))
            return components

        if message_type == "media":
            file_key = str(content.get("file_key", "")).strip()
            file_name = str(content.get("file_name", "")).strip() or "lark_media.mp4"
            if not message_id:
                logger.error("[Lark] 视频消息缺少 message_id")
                return components
            if not file_key:
                logger.error("[Lark] 视频消息缺少 file_key")
                return components
            file_path = await self._download_file_resource_to_temp(
                message_id=message_id,
                file_key=file_key,
                message_type="media",
                file_name=file_name,
                default_suffix=".mp4",
            )
            if file_path:
                components.append(Comp.Video(file=file_path, path=file_path))
            return components

        return components

    async def _build_reply_from_parent_id(
        self,
        parent_message_id: str,
    ) -> Comp.Reply | None:
        if self.lark_api.im is None:
            logger.error("[Lark] API Client im 模块未初始化")
            return None

        request = GetMessageRequest.builder().message_id(parent_message_id).build()
        response = await self.lark_api.im.v1.message.aget(request)
        if not response.success():
            logger.error(
                f"[Lark] 获取引用消息失败 id={parent_message_id}, "
                f"code={response.code}, msg={response.msg}",
            )
            return None

        if response.data is None or not response.data.items:
            logger.error(
                f"[Lark] 引用消息响应为空 id={parent_message_id}",
            )
            return None

        parent_message = response.data.items[0]
        quoted_message_id = parent_message.message_id or parent_message_id
        quoted_sender_id = (
            parent_message.sender.id
            if parent_message.sender and parent_message.sender.id
            else "unknown"
        )
        quoted_time_raw = parent_message.create_time or 0
        quoted_time = (
            quoted_time_raw // 1000
            if isinstance(quoted_time_raw, int) and quoted_time_raw > 10**11
            else quoted_time_raw
        )
        quoted_content = (
            parent_message.body.content if parent_message.body else ""
        ) or ""
        quoted_type = parent_message.msg_type or ""
        quoted_content_json: dict[str, Any] = {}
        if quoted_content:
            try:
                parsed = json.loads(quoted_content)
                if isinstance(parsed, dict):
                    quoted_content_json = parsed
            except json.JSONDecodeError:
                logger.warning(
                    f"[Lark] 解析引用消息内容失败 id={quoted_message_id}",
                )

        quoted_at_map = self._build_at_map(parent_message.mentions)
        quoted_chain = await self._parse_message_components(
            message_id=quoted_message_id,
            message_type=quoted_type,
            content=quoted_content_json,
            at_map=quoted_at_map,
        )
        quoted_text = self._build_message_str_from_components(quoted_chain)
        sender_nickname = (
            quoted_sender_id[:8] if quoted_sender_id != "unknown" else "unknown"
        )

        return Comp.Reply(
            id=quoted_message_id,
            chain=quoted_chain,
            sender_id=quoted_sender_id,
            sender_nickname=sender_nickname,
            time=quoted_time,
            message_str=quoted_text,
            text=quoted_text,
        )

    async def _download_file_resource_to_temp(
        self,
        *,
        message_id: str,
        file_key: str,
        message_type: str,
        file_name: str = "",
        default_suffix: str = ".bin",
    ) -> str | None:
        file_bytes = await self._download_message_resource(
            message_id=message_id,
            file_key=file_key,
            resource_type="file",
        )
        if file_bytes is None:
            return None

        suffix = Path(file_name).suffix if file_name else default_suffix
        temp_dir = Path(get_astrbot_temp_path())
        temp_dir.mkdir(parents=True, exist_ok=True)
        temp_path = (
            temp_dir / f"lark_{message_type}_{file_name}_{uuid4().hex[:4]}{suffix}"
        )
        temp_path.write_bytes(file_bytes)
        return str(temp_path.resolve())

    def _clean_expired_events(self) -> None:
        """清理超过 30 分钟的事件记录"""
        current_time = time.time()
        expired_keys = [
            event_id
            for event_id, timestamp in self.event_id_timestamps.items()
            if current_time - timestamp > 1800
        ]
        for event_id in expired_keys:
            del self.event_id_timestamps[event_id]

    def _is_duplicate_event(self, event_id: str) -> bool:
        """检查事件是否重复

        Args:
            event_id: 事件ID

        Returns:
            True 表示重复事件，False 表示新事件
        """
        self._clean_expired_events()
        if event_id in self.event_id_timestamps:
            return True
        self.event_id_timestamps[event_id] = time.time()
        return False

    def _clean_expired_message_ids(self) -> None:
        current_time = time.time()
        expired_keys = [
            message_id
            for message_id, timestamp in self.message_id_timestamps.items()
            if current_time - timestamp > self.MESSAGE_ID_DEDUPE_TTL_SECONDS
        ]
        for message_id in expired_keys:
            del self.message_id_timestamps[message_id]

    def _mark_message_id_seen(self, message_id: str) -> bool:
        self._load_persisted_message_ids()
        self._clean_expired_message_ids()
        if message_id in self.message_id_timestamps:
            return False
        self.message_id_timestamps[message_id] = time.time()
        self._persist_message_ids()
        return True

    def _default_message_id_store_path(self) -> Path:
        platform_id = str(self.config.get("id") or self.appid or "lark")
        safe_platform_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", platform_id).strip("_")
        if not safe_platform_id:
            safe_platform_id = "lark"
        return (
            Path(get_astrbot_temp_path())
            / "lark_seen_messages"
            / (f"{safe_platform_id}.json")
        )

    def _default_polling_chat_store_path(self) -> Path:
        platform_id = str(self.config.get("id") or self.appid or "lark")
        safe_platform_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", platform_id).strip("_")
        if not safe_platform_id:
            safe_platform_id = "lark"
        return (
            Path(get_astrbot_temp_path())
            / "lark_polling_chats"
            / (f"{safe_platform_id}.json")
        )

    def _load_persisted_polling_chat_ids(self) -> None:
        store_path = getattr(self, "polling_chat_store_path", None)
        if store_path is None:
            return
        try:
            if not store_path.exists():
                return
            payload = json.loads(store_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Lark.Polling] 读取已学习 chat 缓存失败: %s", exc)
            return
        loaded_chat_ids = (
            payload.get("chat_ids", []) if isinstance(payload, dict) else payload
        )
        if not isinstance(loaded_chat_ids, list):
            return

        chat_ids = getattr(self, "polling_fallback_chat_ids", None)
        if chat_ids is None:
            self.polling_fallback_chat_ids = []
            chat_ids = self.polling_fallback_chat_ids
        for chat_id in loaded_chat_ids:
            normalized = str(chat_id or "").strip()
            if normalized and normalized not in chat_ids:
                chat_ids.append(normalized)

    def _persist_polling_chat_ids(self) -> None:
        store_path = getattr(self, "polling_chat_store_path", None)
        if store_path is None:
            return
        chat_ids = [
            str(chat_id).strip()
            for chat_id in getattr(self, "polling_fallback_chat_ids", [])
            if str(chat_id).strip()
        ]
        try:
            platform_id = self.meta().id
        except Exception:  # noqa: BLE001
            platform_id = str(getattr(self, "appid", "") or "lark")
        try:
            store_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = store_path.with_suffix(store_path.suffix + ".tmp")
            tmp_path.write_text(
                json.dumps(
                    {
                        "platform_id": platform_id,
                        "chat_ids": chat_ids,
                        "updated_at": int(time.time()),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            tmp_path.replace(store_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Lark.Polling] 写入已学习 chat 缓存失败: %s", exc)

    def _load_persisted_message_ids(self) -> None:
        if getattr(self, "_message_id_store_loaded", False):
            return
        self._message_id_store_loaded = True
        store_path = getattr(self, "message_id_store_path", None)
        if store_path is None:
            return
        try:
            if not store_path.exists():
                return
            payload = json.loads(store_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Lark] 读取消息去重缓存失败: %s", exc)
            return
        if not isinstance(payload, dict):
            return
        now = time.time()
        for message_id, timestamp in payload.items():
            if not isinstance(message_id, str):
                continue
            try:
                seen_at = float(timestamp)
            except (TypeError, ValueError):
                continue
            if now - seen_at <= self.MESSAGE_ID_DEDUPE_TTL_SECONDS:
                self.message_id_timestamps[message_id] = seen_at

    def _persist_message_ids(self) -> None:
        store_path = getattr(self, "message_id_store_path", None)
        if store_path is None:
            return
        try:
            store_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = store_path.with_suffix(store_path.suffix + ".tmp")
            tmp_path.write_text(
                json.dumps(self.message_id_timestamps, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp_path.replace(store_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Lark] 写入消息去重缓存失败: %s", exc)

    def _initial_polling_start_time(self, *, has_persisted_seen_messages: bool) -> int:
        if has_persisted_seen_messages:
            return int(time.time() - self.polling_fallback_backfill_seconds)
        return int(time.time())

    @staticmethod
    def _timestamp_to_seconds(value: Any) -> int:
        if value is None:
            return int(time.time())
        try:
            timestamp = int(value)
        except (TypeError, ValueError):
            return int(time.time())
        if timestamp > 10**11:
            return timestamp // 1000
        return timestamp

    @staticmethod
    def _message_body_content(message_item: Any) -> dict[str, Any] | None:
        body = getattr(message_item, "body", None)
        content_raw = getattr(body, "content", "") if body else ""
        if not content_raw:
            content_raw = getattr(message_item, "content", "")
        if isinstance(content_raw, dict):
            return content_raw
        try:
            parsed = json.loads(str(content_raw))
        except json.JSONDecodeError:
            logger.warning("[Lark.Polling] 解析消息内容失败: %s", content_raw)
            return None
        return parsed if isinstance(parsed, dict) else None

    def _message_sender_id(self, message_item: Any) -> str:
        sender = getattr(message_item, "sender", None)
        sender_id = str(getattr(sender, "id", "") or "") if sender else ""
        if not sender_id:
            return ""
        if sender_id == self.appid:
            return ""
        return sender_id

    def _remember_polling_chat_id(self, chat_id: str, *, reason: str) -> bool:
        chat_id = str(chat_id or "").strip()
        if not chat_id or not getattr(self, "polling_fallback_enabled", False):
            return False

        next_start_times = getattr(self, "_polling_next_start_times", None)
        if next_start_times is None:
            self._polling_next_start_times = {}
            next_start_times = self._polling_next_start_times
        next_start_times.setdefault(
            chat_id,
            getattr(self, "_polling_next_start_time", int(time.time())),
        )

        chat_ids = getattr(self, "polling_fallback_chat_ids", None)
        if chat_ids is None:
            self.polling_fallback_chat_ids = []
            chat_ids = self.polling_fallback_chat_ids
        if chat_id in chat_ids:
            self._persist_polling_chat_ids()
            return False

        chat_ids.append(chat_id)
        try:
            platform_id = self.meta().id
        except Exception:  # noqa: BLE001
            platform_id = ""
        logger.info(
            "[Lark.Polling] learned chat platform=%s chat=%s reason=%s total=%s",
            platform_id,
            chat_id,
            reason,
            len(chat_ids),
        )
        self._persist_polling_chat_ids()
        return True

    async def _build_polled_message(self, message_item: Any) -> AstrBotMessage | None:
        message_id = str(getattr(message_item, "message_id", "") or "")
        if not message_id:
            logger.warning("[Lark.Polling] 跳过缺少 message_id 的消息")
            return None

        sender_id = self._message_sender_id(message_item)
        if not sender_id:
            return None

        content_json = self._message_body_content(message_item)
        if content_json is None:
            return None

        msg_type = str(getattr(message_item, "msg_type", "") or "unknown")
        parsed_components = await self._parse_message_components(
            message_id=message_id,
            message_type=msg_type,
            content=content_json,
            at_map=self._build_at_map(getattr(message_item, "mentions", None)),
        )
        message_str = self._build_message_str_from_components(parsed_components)
        if not message_str:
            return None

        chat_id = str(getattr(message_item, "chat_id", "") or "")
        chat_type = str(getattr(message_item, "chat_type", "") or "")
        abm = AstrBotMessage()
        abm.timestamp = self._timestamp_to_seconds(
            getattr(message_item, "create_time", None)
        )
        abm.message = parsed_components
        abm.type = (
            MessageType.GROUP_MESSAGE
            if chat_type == "group"
            else MessageType.FRIEND_MESSAGE
        )
        if abm.type == MessageType.GROUP_MESSAGE:
            abm.group_id = chat_id
        abm.self_id = self.bot_name
        abm.message_str = message_str
        abm.message_id = message_id
        abm.raw_message = message_item
        abm.sender = MessageMember(user_id=sender_id, nickname=sender_id[:8])
        abm.session_id = chat_id if abm.type == MessageType.GROUP_MESSAGE else sender_id
        return abm

    async def _poll_lark_chat_messages(self, chat_id: str) -> None:
        if self.lark_api.im is None:
            logger.error("[Lark.Polling] API Client im 模块未初始化")
            return

        end_time = int(time.time())
        next_start_times = getattr(self, "_polling_next_start_times", None)
        if next_start_times is None:
            self._polling_next_start_times = {}
            next_start_times = self._polling_next_start_times
        start_time = max(
            0,
            int(
                next_start_times.get(
                    chat_id,
                    getattr(self, "_polling_next_start_time", end_time),
                )
            ),
        )
        page_token = ""
        fetched_pages = 0
        max_create_time = start_time

        while True:
            builder = (
                ListMessageRequest.builder()
                .container_id_type("chat")
                .container_id(chat_id)
                .start_time(str(start_time))
                .end_time(str(end_time))
                .sort_type("ByCreateTimeAsc")
                .page_size(self.polling_fallback_page_size)
            )
            if page_token:
                builder = builder.page_token(page_token)
            response = await self.lark_api.im.v1.message.alist(builder.build())
            if not response.success():
                logger.warning(
                    "[Lark.Polling] 拉取消息失败 platform=%s chat=%s code=%s msg=%s",
                    self.meta().id,
                    chat_id,
                    response.code,
                    response.msg,
                )
                return

            data = getattr(response, "data", None)
            items = list(getattr(data, "items", None) or [])
            for item in items:
                message_id = str(getattr(item, "message_id", "") or "")
                create_time = self._timestamp_to_seconds(
                    getattr(item, "create_time", None)
                )
                max_create_time = max(max_create_time, create_time)
                if not message_id or not self._mark_message_id_seen(message_id):
                    continue
                abm = await self._build_polled_message(item)
                if abm is None:
                    continue
                logger.info(
                    "[Lark.Polling] event queued platform=%s message_id=%s session=%s chat=%s",
                    self.meta().id,
                    message_id,
                    str(abm.session_id)[:24],
                    chat_id,
                )
                await self.handle_msg(abm)

            fetched_pages += 1
            page_token = str(getattr(data, "page_token", "") or "")
            has_more = bool(getattr(data, "has_more", False))
            if not has_more or not page_token or fetched_pages >= 5:
                break

        next_start_times[chat_id] = max(start_time, max_create_time - 5)
        self._polling_next_start_time = min(next_start_times.values())

    async def _polling_fallback_loop(self) -> None:
        logger.info(
            "[Lark.Polling] starting platform=%s chats=%s interval=%ss backfill=%ss",
            self.meta().id,
            len(self.polling_fallback_chat_ids),
            self.polling_fallback_interval,
            self.polling_fallback_backfill_seconds,
        )
        while True:
            try:
                for chat_id in self.polling_fallback_chat_ids:
                    await self._poll_lark_chat_messages(chat_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("[Lark.Polling] 轮询兜底异常: %s", exc, exc_info=True)
            await asyncio.sleep(self.polling_fallback_interval)

    async def send_by_session(
        self,
        session: MessageSesion,
        message_chain: MessageChain,
    ) -> None:
        if session.message_type == MessageType.GROUP_MESSAGE:
            id_type = "chat_id"
            receive_id = session.session_id
            if "%" in receive_id:
                receive_id = receive_id.split("%")[1]
        else:
            id_type = "open_id"
            receive_id = session.session_id

        # 复用 LarkMessageEvent 中的通用发送逻辑
        await LarkMessageEvent.send_message_chain(
            message_chain,
            self.lark_api,
            receive_id=receive_id,
            receive_id_type=id_type,
        )

        await super().send_by_session(session, message_chain)

    def meta(self) -> PlatformMetadata:
        return PlatformMetadata(
            name="lark",
            description="飞书机器人官方 API 适配器",
            id=cast(str, self.config.get("id")),
            support_streaming_message=True,
        )

    async def convert_msg(self, event: lark.im.v1.P2ImMessageReceiveV1) -> None:
        if event.event is None:
            logger.debug("[Lark] 收到空事件(event.event is None)")
            return
        message = event.event.message
        if message is None:
            logger.debug("[Lark] 事件中没有消息体(message is None)")
            return

        abm = AstrBotMessage()

        # ─── 临时调试：打印 raw chat_type / chat_id，定位群消息为何被当私聊 ───
        try:
            logger.info(
                f"[Lark.DEBUG] raw event: chat_type={message.chat_type!r} "
                f"chat_id={message.chat_id!r} message_id={message.message_id!r} "
                f"sender_open_id={(event.event.sender.sender_id.open_id if event.event.sender and event.event.sender.sender_id else None)!r}"
            )
        except Exception as _exc:
            logger.warning(f"[Lark.DEBUG] 打印 raw 字段失败: {_exc}")
        # ───────────────────────────────────────────────────────────

        if message.create_time:
            abm.timestamp = int(message.create_time) // 1000
        else:
            abm.timestamp = int(time.time())
        if message.chat_id:
            self._remember_polling_chat_id(
                message.chat_id,
                reason=f"socket_{message.chat_type or 'unknown'}",
            )
        abm.message = []
        abm.type = (
            MessageType.GROUP_MESSAGE
            if message.chat_type == "group"
            else MessageType.FRIEND_MESSAGE
        )
        if message.chat_type == "group":
            abm.group_id = message.chat_id
        abm.self_id = self.bot_open_id or self.bot_name
        abm.message_str = ""

        at_list = {}
        if message.parent_id:
            reply_seg = await self._build_reply_from_parent_id(message.parent_id)
            if reply_seg:
                abm.message.append(reply_seg)

        if message.mentions:
            for m in message.mentions:
                if m.id is None:
                    continue
                # 飞书 open_id 可能是 None，这里做个防护
                open_id = m.id.open_id if m.id.open_id else ""
                at_list[m.key] = Comp.At(qq=open_id, name=m.name)

                if (self.bot_open_id and open_id == self.bot_open_id) or (
                    m.name == self.bot_name
                ):
                    abm.self_id = open_id or self.bot_open_id or self.bot_name

        if message.content is None:
            logger.warning("[Lark] 消息内容为空")
            return

        try:
            content_json_b = json.loads(message.content)
        except json.JSONDecodeError:
            logger.error(f"[Lark] 解析消息内容失败: {message.content}")
            return

        if not isinstance(content_json_b, dict):
            logger.error(f"[Lark] 消息内容不是 JSON Object: {message.content}")
            return

        logger.debug(f"[Lark] 解析消息内容: {content_json_b}")
        parsed_components = await self._parse_message_components(
            message_id=message.message_id,
            message_type=message.message_type or "unknown",
            content=content_json_b,
            at_map=at_list,
        )
        abm.message.extend(parsed_components)
        abm.message_str = self._build_message_str_from_components(parsed_components)

        if message.message_id is None:
            logger.error("[Lark] 消息缺少 message_id")
            return

        if not self._mark_message_id_seen(message.message_id):
            logger.info(
                "[Lark.Socket] 跳过重复消息 platform=%s message_id=%s",
                self.meta().id,
                message.message_id,
            )
            return

        if (
            event.event.sender is None
            or event.event.sender.sender_id is None
            or event.event.sender.sender_id.open_id is None
        ):
            logger.error("[Lark] 消息发送者信息不完整")
            return

        abm.message_id = message.message_id
        abm.raw_message = message
        abm.sender = MessageMember(
            user_id=event.event.sender.sender_id.open_id,
            nickname=event.event.sender.sender_id.open_id[:8],
        )
        if abm.type == MessageType.GROUP_MESSAGE:
            abm.session_id = abm.group_id
        else:
            abm.session_id = abm.sender.user_id

        await self.handle_msg(abm)

    async def handle_msg(self, abm: AstrBotMessage) -> None:
        if await self._maybe_buffer_multimodal_message(abm):
            return
        await self._enqueue_msg(abm)

    async def _enqueue_msg(self, abm: AstrBotMessage) -> None:
        event = LarkMessageEvent(
            message_str=abm.message_str,
            message_obj=abm,
            platform_meta=self.meta(),
            session_id=abm.session_id,
            bot=self.lark_api,
        )

        self._event_queue.put_nowait(event)
        self._last_event_at = time.time()
        try:
            queue_size = self._event_queue.qsize()
        except NotImplementedError:
            queue_size = -1
        logger.info(
            "[Lark.Socket] event queued platform=%s message_type=%s message_id=%s session=%s queue_size=%s",
            self.meta().id,
            abm.type.name if hasattr(abm.type, "name") else str(abm.type),
            abm.message_id,
            str(abm.session_id)[:24],
            queue_size,
        )

    @staticmethod
    def _is_mergeable_multimodal_fragment(abm: AstrBotMessage) -> bool:
        message_str = str(getattr(abm, "message_str", "") or "")
        if message_str.startswith("__card_action__:"):
            return False

        components = list(getattr(abm, "message", []) or [])
        if not components:
            return False

        return all(
            isinstance(comp, (Comp.At, Comp.File, Comp.Image, Comp.Plain, Comp.Reply))
            for comp in components
        )

    def _pending_multimodal_key(self, abm: AstrBotMessage) -> str:
        message_type = (
            abm.type.name
            if hasattr(abm.type, "name")
            else str(getattr(abm, "type", ""))
        )
        sender = getattr(getattr(abm, "sender", None), "user_id", "")
        return f"{message_type}:{getattr(abm, 'session_id', '')}:{sender}"

    @staticmethod
    def _has_file_component(abm: AstrBotMessage | None) -> bool:
        if abm is None:
            return False
        return any(isinstance(comp, Comp.File) for comp in (abm.message or []))

    def _merge_window_for_fragments(
        self,
        current: AstrBotMessage,
        previous: AstrBotMessage | None = None,
    ) -> float:
        base_window = float(
            getattr(
                self,
                "multimodal_merge_window_seconds",
                getattr(self, "image_text_merge_window_seconds", 0),
            )
            or 0
        )
        if self._has_file_component(current) or self._has_file_component(previous):
            return max(
                base_window,
                float(
                    getattr(
                        self,
                        "file_multimodal_merge_window_seconds",
                        getattr(self, "file_text_merge_window_seconds", 60),
                    )
                    or 60
                ),
            )
        return base_window

    async def _maybe_buffer_multimodal_message(self, abm: AstrBotMessage) -> bool:
        window = self._merge_window_for_fragments(abm)
        pending_messages = getattr(self, "_pending_multimodal_messages", None)
        if pending_messages is None:
            self._pending_multimodal_messages = {}
            pending_messages = self._pending_multimodal_messages

        if window <= 0 or not self._is_mergeable_multimodal_fragment(abm):
            return False

        pending_key = self._pending_multimodal_key(abm)
        previous = pending_messages.pop(pending_key, None)
        if previous:
            previous_abm, previous_task, _previous_window = previous
            previous_task.cancel()
            window = self._merge_window_for_fragments(abm, previous_abm)
            abm.message = [*previous_abm.message, *abm.message]
            abm.message_str = self._build_message_str_from_components(abm.message)
            logger.info(
                "[Lark] merged adjacent multimodal fragments platform=%s session=%s",
                self.meta().id,
                str(getattr(abm, "session_id", ""))[:24],
            )

        task = asyncio.create_task(
            self._flush_pending_multimodal_message(pending_key, window)
        )
        pending_messages[pending_key] = (abm, task, window)
        logger.info(
            "[Lark] buffered multimodal fragment platform=%s session=%s window=%.1fs",
            self.meta().id,
            str(getattr(abm, "session_id", ""))[:24],
            window,
        )
        return True

    async def _flush_pending_multimodal_message(
        self,
        pending_key: str,
        window: float,
    ) -> None:
        try:
            await asyncio.sleep(window)
        except asyncio.CancelledError:
            return

        pending_messages = getattr(self, "_pending_multimodal_messages", {})
        pending = pending_messages.pop(pending_key, None)
        if not pending:
            return
        abm, _task, _window = pending
        await self._enqueue_msg(abm)

    async def convert_card_action(self, event) -> None:
        """卡片按钮回调 → 转 AstrBotMessage 投到事件队列。

        plugin 用 message_str 前缀 `__card_action__:` 识别（JSON payload）。
        payload 形如：{"action": "select_dept", "value": "marketing", ...}
        来自卡片按钮 value 字段。
        """
        try:
            data = event.event
            if data is None:
                return
            operator = getattr(data, "operator", None)
            action = getattr(data, "action", None)
            ctx = getattr(data, "context", None)
            if operator is None or action is None:
                logger.warning("[Lark] card_action 事件缺 operator/action 字段")
                return

            open_id = (
                getattr(operator, "open_id", None)
                or getattr(operator, "user_id", None)
                or ""
            )
            if not open_id:
                logger.warning("[Lark] card_action 找不到 operator open_id")
                return

            # action.value 是 dict（按钮 value 字段）
            action_value = getattr(action, "value", None) or {}
            # form_value 是表单提交（input 等）
            form_value = getattr(action, "form_value", None) or {}
            input_value = getattr(action, "input_value", None) or ""

            # 拼合 payload
            payload = {
                "value": action_value,
                "form_value": form_value,
                "input_value": input_value,
                "tag": getattr(action, "tag", None),
                "name": getattr(action, "name", None),
                "open_chat_id": getattr(ctx, "open_chat_id", None) if ctx else None,
                "open_message_id": getattr(ctx, "open_message_id", None)
                if ctx
                else None,
                "token": getattr(data, "token", None),
            }

            abm = AstrBotMessage()
            abm.timestamp = int(time.time())
            abm.message = []
            abm.type = MessageType.FRIEND_MESSAGE
            abm.self_id = self.bot_name
            abm.message_str = "__card_action__:" + json.dumps(
                payload, ensure_ascii=False
            )
            # Use real open_message_id from card context when available (prevents 99992354 invalid id errors).
            # The synthetic "card_action_..." was being passed as open_message_id to Feishu APIs expecting real "om_..." card message ids.
            # Fallback to synthetic only if no original id (for pure action identification).
            original_msg_id = payload.get("open_message_id")
            abm.message_id = (
                original_msg_id
                or f"card_action_{int(time.time() * 1000)}_{open_id[:8]}"
            )
            abm.raw_message = data
            abm.is_card_action = True
            abm.card_action_payload = payload
            abm.sender = MessageMember(user_id=open_id, nickname=open_id[:8])
            abm.session_id = open_id
            if original_msg_id:
                abm.original_card_message_id = (
                    original_msg_id  # for downstream card update logic
                )

            logger.info(
                "[Lark.CardAction] open_id=%s value=%s",
                open_id[:12],
                action_value,
            )
            await self.handle_msg(abm)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Lark] convert_card_action 异常：%s", exc)

    async def convert_p2p_chat_entered(self, event) -> None:
        try:
            data = event.event
            if data is None:
                return
            operator_id = getattr(data, "operator_id", None)
            open_id = (
                getattr(operator_id, "open_id", None)
                or getattr(operator_id, "user_id", None)
                or ""
            )
            if not open_id:
                logger.warning("[Lark] p2p chat entered 事件找不到 operator open_id")
                return

            logger.info(
                "[Lark.P2PEntered] open_id=%s chat_id=%s",
                open_id[:12],
                getattr(data, "chat_id", ""),
            )
            self._remember_polling_chat_id(
                str(getattr(data, "chat_id", "") or ""),
                reason="p2p_entered",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Lark] convert_p2p_chat_entered 异常：%s", exc)

    async def handle_webhook_event(self, event_data: dict) -> None:
        """处理 Webhook 事件

        Args:
            event_data: Webhook 事件数据
        """
        try:
            header = event_data.get("header", {})
            event_id = header.get("event_id", "")
            if event_id and self._is_duplicate_event(event_id):
                logger.debug(f"[Lark Webhook] 跳过重复事件: {event_id}")
                return
            event_type = header.get("event_type", "")
            if event_type == "im.message.receive_v1":
                processor = P2ImMessageReceiveV1Processor(self.do_v2_msg_event)
                data = (processor.type())(event_data)
                processor.do(data)
            elif event_type in (
                "im.chat.access_event.bot_p2p_chat_entered_v1",
                "p2.im.chat.access_event.bot_p2p_chat_entered_v1",
            ):
                processor = P2ImChatAccessEventBotP2pChatEnteredV1Processor(
                    self.do_p2p_chat_entered_event
                )
                data = (processor.type())(event_data)
                processor.do(data)
            else:
                logger.debug(f"[Lark Webhook] 未处理的事件类型: {event_type}")
        except Exception as e:
            logger.error(f"[Lark Webhook] 处理事件失败: {e}", exc_info=True)

    async def run(self) -> None:
        try:
            await self._refresh_bot_info()
        except Exception as e:
            logger.error(f"[Lark] 启动时获取机器人信息失败: {e}", exc_info=True)

        if self.connection_mode == "webhook":
            # Webhook 模式
            if self.webhook_server is None:
                logger.error("[Lark] Webhook 模式已启用，但 webhook_server 未初始化")
                return

            webhook_uuid = self.config.get("webhook_uuid")
            if webhook_uuid:
                log_webhook_info(f"{self.meta().id}(飞书 Webhook)", webhook_uuid)
            else:
                logger.warning("[Lark] Webhook 模式已启用，但未配置 webhook_uuid")
        else:
            # 长连接模式
            logger.info("[Lark.Socket] starting platform=%s", self.meta().id)
            await self.client._connect()
            self._mark_socket_connected(source="initial")
            if self.polling_fallback_enabled and self.polling_fallback_chat_ids:
                self._polling_fallback_task = asyncio.create_task(
                    self._polling_fallback_loop()
                )

    async def webhook_callback(self, request: Any) -> Any:
        """统一 Webhook 回调入口"""
        if not self.webhook_server:
            return {"error": "Webhook server not initialized"}, 500

        return await self.webhook_server.handle_callback(request)

    async def _refresh_bot_info(self) -> None:
        bot_info = await request_lark_bot_info(
            domain=self.domain,
            app_id=self.appid,
            app_secret=self.appsecret,
        )
        if bot_info.app_name:
            self.bot_name = bot_info.app_name
        if bot_info.open_id:
            self.bot_open_id = bot_info.open_id

    async def terminate(self) -> None:
        if self._polling_fallback_task is not None:
            self._polling_fallback_task.cancel()
            try:
                await self._polling_fallback_task
            except asyncio.CancelledError:
                pass
            self._polling_fallback_task = None
        if self.connection_mode == "socket":
            await self.client._disconnect()
        logger.info("飞书(Lark) 适配器已关闭")

    def get_client(self) -> lark.ws.Client:
        return self.client

    def unified_webhook(self) -> bool:
        return bool(
            self.config.get("lark_connection_mode", "") == "webhook"
            and self.config.get("webhook_uuid")
        )
