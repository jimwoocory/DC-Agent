"""创建飞书临时群 + 邀请成员。"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from lark_oapi.api.im.v1 import (
    CreateChatMembersRequest,
    CreateChatMembersRequestBody,
    CreateChatRequest,
    CreateChatRequestBody,
)

from .contracts import ChatCreationRequest, ChatCreationResult

if TYPE_CHECKING:
    from dc_engines.feishu_reader import FeishuCredentials

logger = logging.getLogger(__name__)


class ChatCreator:
    """轻量飞书建群器。

    收编后内部 ``self._client`` 来自 ``dc_engines.feishu_hub``（单例）——
    跟 feishu_reader / nas_sync.feishu_sync / employee_directory.sync 共用
    同一个 ``lark.Client``，token cache 共享、调用统计集中。

    向后兼容：__init__ 仍接受 ``credentials`` 参数；如果传了就用，没传就从
    hub 拿。
    """

    def __init__(self, credentials: FeishuCredentials | None = None) -> None:
        from dc_engines.feishu_hub import get_client, get_credentials

        if credentials is None:
            hub_creds = get_credentials()
            self.credentials = hub_creds  # type: ignore[assignment]
        else:
            self.credentials = credentials
        self._client = get_client()

    @property
    def enabled(self) -> bool:
        return bool(
            self._client is not None
            and self.credentials
            and self.credentials.enable
            and self.credentials.app_id
            and self.credentials.app_secret
        )

    async def create_group_chat(
        self, request: ChatCreationRequest
    ) -> ChatCreationResult:
        """创建临时群。失败返回 ChatCreationResult(success=False, error=...)。"""
        if not self.enabled:
            return ChatCreationResult(
                success=False, error="Feishu credentials disabled"
            )
        if not request.name.strip():
            return ChatCreationResult(success=False, error="群名不能为空")
        if not request.owner_open_id:
            return ChatCreationResult(success=False, error="缺 owner_open_id")

        # 1) 创建群 + owner + 初始成员（一次调用最多 50 个，飞书 API 限制）
        first_batch = request.member_open_ids[:50]
        rest_batches = [
            request.member_open_ids[i : i + 50]
            for i in range(50, len(request.member_open_ids), 50)
        ]

        try:
            body_builder = (
                CreateChatRequestBody.builder()
                .name(request.name.strip())
                .owner_id(request.owner_open_id)
                .chat_mode(request.chat_mode)
                .chat_type(request.chat_type)
                .external(request.external)
            )
            if request.description:
                body_builder = body_builder.description(request.description[:200])
            if first_batch:
                body_builder = body_builder.user_id_list(first_batch)

            req_builder = (
                CreateChatRequest.builder()
                .user_id_type("open_id")
                .request_body(body_builder.build())
            )
            if request.set_bot_manager:
                req_builder = req_builder.set_bot_manager(True)

            req = req_builder.build()
            resp = await self._client.im.v1.chat.acreate(req)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[feishu_writer] create chat exception: %s", exc)
            return ChatCreationResult(
                success=False, error=f"{type(exc).__name__}: {exc}"
            )

        if not resp.success():
            code = getattr(resp, "code", "?")
            msg = getattr(resp, "msg", "?")
            logger.warning("[feishu_writer] create chat 失败 code=%s msg=%s", code, msg)
            return ChatCreationResult(
                success=False,
                error=f"飞书 API 错误 code={code} msg={msg}",
            )

        chat_id = getattr(resp.data, "chat_id", None) if resp.data else None
        invited_count = len(first_batch)
        invalid_ids: list[str] = []

        # 2) 剩余成员分批邀请
        for batch in rest_batches:
            ok, invalid = await self._invite_members(chat_id, batch)
            if ok > 0:
                invited_count += ok
            invalid_ids.extend(invalid)

        chat_url = f"https://feishu.cn/chat/{chat_id}" if chat_id else None
        return ChatCreationResult(
            success=True,
            chat_id=chat_id,
            chat_url=chat_url,
            invited_count=invited_count,
            invalid_member_ids=invalid_ids,
        )

    async def invite_members(
        self, chat_id: str, member_open_ids: list[str]
    ) -> tuple[int, list[str], str | None]:
        """向已有群拉人。

        返回 (成功数, invalid_id_list, error_msg)。error_msg=None 表示无 API 错误，
        但 invalid_id_list 可能非空（已在群/退群/被踢/open_id 失效等情况）。
        分批发送（飞书单次最多 50）。
        """
        if not self.enabled:
            return 0, [], "Feishu credentials disabled"
        if not chat_id:
            return 0, [], "chat_id 不能为空"
        if not member_open_ids:
            return 0, [], None

        # 去重保序
        seen: set[str] = set()
        deduped = [m for m in member_open_ids if not (m in seen or seen.add(m))]

        total_ok = 0
        invalid_all: list[str] = []
        for i in range(0, len(deduped), 50):
            batch = deduped[i : i + 50]
            ok, invalid, err = await self._invite_batch(chat_id, batch)
            if err:
                return total_ok, invalid_all + invalid, err
            total_ok += ok
            invalid_all.extend(invalid)
        return total_ok, invalid_all, None

    async def _invite_members(
        self, chat_id: str | None, member_open_ids: list[str]
    ) -> tuple[int, list[str]]:
        """内部：建群流程里给 50+ 成员补邀请用。返回 (ok, invalid)。"""
        if not chat_id or not member_open_ids:
            return 0, []
        ok, invalid, _err = await self._invite_batch(chat_id, member_open_ids)
        return ok, invalid

    async def _invite_batch(
        self, chat_id: str, member_open_ids: list[str]
    ) -> tuple[int, list[str], str | None]:
        try:
            body = (
                CreateChatMembersRequestBody.builder().id_list(member_open_ids).build()
            )
            req = (
                CreateChatMembersRequest.builder()
                .chat_id(chat_id)
                .member_id_type("open_id")
                .succeed_type(1)  # 部分成功也算成功
                .request_body(body)
                .build()
            )
            resp = await self._client.im.v1.chat_members.acreate(req)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[feishu_writer] invite exception: %s", exc)
            return 0, member_open_ids, f"{type(exc).__name__}: {exc}"

        if not resp.success():
            code = getattr(resp, "code", "?")
            msg = getattr(resp, "msg", "?")
            logger.warning("[feishu_writer] invite 失败 code=%s msg=%s", code, msg)
            return 0, member_open_ids, f"飞书 API code={code} msg={msg}"

        invalid = list(getattr(resp.data, "invalid_id_list", []) or [])
        ok = len(member_open_ids) - len(invalid)
        return ok, invalid, None
