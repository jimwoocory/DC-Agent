"""FeishuCardStreamer · 飞书卡片流式更新核心。

设计为**完全通用**，不依赖 AstrBot / Hermes 任何内部模块。
调用方负责：
- 构建 lark.Client 传进来（用同一个 app_id/app_secret）
- 提供 chat_id（私聊 ou_xxx 或群聊 oc_xxx）
- 提供初始卡片 dict（用 templates 里的 builder 即可）

线程模型：
- 单 streamer 实例可同时管理多个 stream（一个 stream = 一张卡片 + 一个
  后台 asyncio task 周期性 update）。
- stream_id 用 message_id（飞书自己分配，全局唯一）。
- 关闭 stream 时取消 asyncio task。

错误处理：
- 飞书 API 失败时静默 log，**不抛**给调用方（卡片更新失败不应该影响
  业务主流程）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageReactionRequest,
    CreateMessageReactionRequestBody,
    CreateMessageRequest,
    CreateMessageRequestBody,
    Emoji,
    PatchMessageRequest,
    PatchMessageRequestBody,
)

logger = logging.getLogger("feishu_card_streamer")


@dataclass
class CardStream:
    """单个卡片流的状态。"""

    message_id: str
    """飞书消息 ID（同时是 stream_id）"""
    chat_id: str
    """目标会话 ID（私聊 ou_xxx / 群聊 oc_xxx）"""
    receive_id_type: str
    """飞书要的 ID 类型：'open_id' / 'chat_id' / 'union_id' / 'user_id' / 'email'"""
    created_at: float = field(default_factory=time.time)
    """创建时间戳，用于计算 elapsed"""
    last_card: dict[str, Any] = field(default_factory=dict)
    """最近一次的卡片内容（patch 用）"""
    auto_update_task: asyncio.Task | None = None
    """后台定时 update 的 task"""
    finalized: bool = False
    """是否已 finalize（避免重复终态）"""

    @property
    def elapsed_sec(self) -> float:
        return time.time() - self.created_at


class FeishuCardStreamer:
    """飞书卡片流式发送/更新引擎。"""

    def __init__(self, lark_client: lark.Client) -> None:
        if lark_client is None:
            raise ValueError("lark_client is required")
        self._client = lark_client
        self._streams: dict[str, CardStream] = {}

    # ─────────────── 启动卡片 ───────────────

    async def start(
        self,
        *,
        chat_id: str,
        receive_id_type: str,
        card: dict[str, Any],
    ) -> CardStream | None:
        """发首张卡片，返回 CardStream（含 message_id）。失败返 None。"""
        try:
            body = (
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("interactive")
                .content(json.dumps(card, ensure_ascii=False))
                .build()
            )
            req = (
                CreateMessageRequest.builder()
                .receive_id_type(receive_id_type)
                .request_body(body)
                .build()
            )
            resp = await self._client.im.v1.message.acreate(req)
            if not resp.success():
                logger.warning(
                    "[streamer] 创建卡片失败 code=%s msg=%s",
                    resp.code,
                    resp.msg,
                )
                return None
            if resp.data is None or not resp.data.message_id:
                logger.warning("[streamer] 创建卡片返回空 message_id")
                return None
            stream = CardStream(
                message_id=resp.data.message_id,
                chat_id=chat_id,
                receive_id_type=receive_id_type,
                last_card=card,
            )
            self._streams[stream.message_id] = stream
            logger.info(
                "[streamer] 卡片已创建 message_id=%s chat_id=%s",
                stream.message_id,
                chat_id[:20],
            )
            return stream
        except Exception as exc:  # noqa: BLE001
            logger.warning("[streamer] start 异常：%s", exc)
            return None

    # ─────────────── 更新卡片 ───────────────

    async def update(
        self,
        message_id: str,
        card: dict[str, Any],
    ) -> bool:
        """patch 已有卡片为新内容。"""
        stream = self._streams.get(message_id)
        if stream is None:
            logger.debug("[streamer] update 时 stream 不存在 message_id=%s", message_id)
            return False
        if stream.finalized:
            return False
        try:
            body = (
                PatchMessageRequestBody.builder()
                .content(json.dumps(card, ensure_ascii=False))
                .build()
            )
            req = (
                PatchMessageRequest.builder()
                .message_id(message_id)
                .request_body(body)
                .build()
            )
            resp = await self._client.im.v1.message.apatch(req)
            if not resp.success():
                logger.warning(
                    "[streamer] update 失败 code=%s msg=%s",
                    resp.code,
                    resp.msg,
                )
                return False
            stream.last_card = card
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("[streamer] update 异常：%s", exc)
            return False

    # ─────────────── 终态 ───────────────

    async def finalize(
        self,
        message_id: str,
        card: dict[str, Any],
    ) -> bool:
        """终态：停掉后台 update task + 最后 patch 一次。"""
        stream = self._streams.get(message_id)
        if stream is None:
            return False
        if stream.finalized:
            return True

        # 先停后台 task（避免在 finalize 后再被覆盖）
        if stream.auto_update_task and not stream.auto_update_task.done():
            stream.auto_update_task.cancel()
            try:
                await stream.auto_update_task
            except (asyncio.CancelledError, Exception):
                pass

        stream.finalized = True
        # 最后一次 patch（绕过 finalized 检查，直接调底层）
        try:
            body = (
                PatchMessageRequestBody.builder()
                .content(json.dumps(card, ensure_ascii=False))
                .build()
            )
            req = (
                PatchMessageRequest.builder()
                .message_id(message_id)
                .request_body(body)
                .build()
            )
            resp = await self._client.im.v1.message.apatch(req)
            if not resp.success():
                logger.warning(
                    "[streamer] finalize patch 失败 code=%s msg=%s",
                    resp.code,
                    resp.msg,
                )
                return False
            stream.last_card = card
            logger.info(
                "[streamer] 卡片终态 message_id=%s elapsed=%.1fs",
                message_id,
                stream.elapsed_sec,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("[streamer] finalize 异常：%s", exc)
            return False
        finally:
            # 释放引用（避免 dict 永远增长）
            self._streams.pop(message_id, None)

    # ─────────────── 后台自动 update ───────────────

    def start_auto_update(
        self,
        message_id: str,
        builder: Callable[[CardStream], Awaitable[dict[str, Any]] | dict[str, Any]],
        interval_sec: float = 15.0,
    ) -> asyncio.Task | None:
        """启动后台 task，每 interval_sec 调 builder(stream) 生成新卡片 + patch。

        builder 可以是同步 / 异步函数，接收 stream 自己（能拿到 elapsed_sec、
        last_card 等状态），返回新卡片 dict。

        Returns:
            asyncio.Task 对象（可手动 cancel；finalize 时会自动 cancel）
        """
        stream = self._streams.get(message_id)
        if stream is None or stream.finalized:
            return None
        if stream.auto_update_task and not stream.auto_update_task.done():
            return stream.auto_update_task  # 已有，不重复起

        async def _loop():
            while True:
                try:
                    await asyncio.sleep(interval_sec)
                    if stream.finalized:
                        return
                    result = builder(stream)
                    new_card = await result if asyncio.iscoroutine(result) else result
                    if not isinstance(new_card, dict):
                        continue
                    await self.update(message_id, new_card)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    logger.debug("[streamer] auto_update 单轮异常：%s", exc)

        task = asyncio.create_task(_loop(), name=f"streamer-update-{message_id[:8]}")
        stream.auto_update_task = task
        return task

    # ─────────────── emoji 反应 ───────────────

    async def react(
        self,
        message_id: str,
        emoji: str,
    ) -> bool:
        """给消息贴 emoji 反应（适用于响应**用户**消息，不一定是自己的卡片）。

        Args:
            emoji: 飞书 emoji_type 字符串，如 'EYES' / 'CYCLE' / 'DONE' / 'CROSS'
                   常用值参考: https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/message-reaction/emoji-type
        """
        try:
            body = (
                CreateMessageReactionRequestBody.builder()
                .reaction_type(Emoji.builder().emoji_type(emoji).build())
                .build()
            )
            req = (
                CreateMessageReactionRequest.builder()
                .message_id(message_id)
                .request_body(body)
                .build()
            )
            resp = await self._client.im.v1.message_reaction.acreate(req)
            if not resp.success():
                logger.debug(
                    "[streamer] react 失败 emoji=%s code=%s msg=%s",
                    emoji,
                    resp.code,
                    resp.msg,
                )
                return False
            return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("[streamer] react 异常：%s", exc)
            return False

    # ─────────────── 工具方法 ───────────────

    def get_stream(self, message_id: str) -> CardStream | None:
        return self._streams.get(message_id)

    def active_count(self) -> int:
        return sum(1 for s in self._streams.values() if not s.finalized)
