"""临时建群 Star 插件（P2）。

`/chat new <群名> <成员1> <成员2> ...`

成员可以是：
- `ou_xxx` 原始飞书 open_id
- `@用户名` 通过 employee_directory.display_name 查档
- `用户名` 同上

发起人（owner）默认是命令的发送者；如果命令发送者不在飞书平台（如 QQ），不允许建群。

凭证复用 ``data/feishu_whitelist.yaml`` 里的 ``feishu.app_id/app_secret``，
所以**前置条件**：ops 在飞书后台为 app 勾选 ``im:chat`` + ``im:chat.member`` 权限。
"""

from __future__ import annotations

from pathlib import Path

from dc_engines.feishu_reader.whitelist import load_whitelist
from dc_engines.feishu_writer import ChatCreationRequest, ChatCreator

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.star import Context, Star, register


@register(
    "chat_creator_plugin",
    "dc_agent",
    "临时建群（P2 接待机器人配套）：/chat new <名> <成员...>",
    "1.0.0",
)
class ChatCreatorPlugin(Star):
    def __init__(self, context: Context) -> None:
        super().__init__(context)
        self.creator: ChatCreator | None = None

    async def initialize(self) -> None:
        wl_path = Path(__file__).resolve().parents[3] / "data" / "feishu_whitelist.yaml"
        _, creds = load_whitelist(wl_path)
        if creds and creds.enable:
            try:
                self.creator = ChatCreator(creds)
                logger.info("[chat_creator] 启动 mode=v1（凭证已就绪，建群可用）")
            except Exception as exc:  # noqa: BLE001
                logger.warning("[chat_creator] ChatCreator 初始化失败：%s", exc)
        else:
            logger.info(
                "[chat_creator] 启动 mode=disabled（缺 feishu 凭证；编辑 %s 启用）",
                wl_path,
            )

    def _reply(self, event: AstrMessageEvent, text: str) -> None:
        event.set_result(MessageEventResult().message(text).use_t2i(False).stop_event())

    @filter.event_message_type(EventMessageType.PRIVATE_MESSAGE, priority=90)
    async def group_help_request(self, event: AstrMessageEvent):
        text = (event.message_str or "").strip()
        if not self._is_group_help_request(text):
            return
        self._reply(event, self._group_help_text())

    def _is_group_help_request(self, text: str) -> bool:
        compact = "".join(text.split())
        if not compact:
            return False
        has_group = "群" in compact or "群聊" in compact
        wants_bot = any(
            word in compact for word in ("拉你", "拉我", "加你", "邀请你", "进群")
        )
        return has_group and wants_bot

    def _group_help_text(self) -> str:
        return (
            "可以，有两种方式：\n"
            "1. 已有飞书群：在群设置里添加机器人「巅池-Agent小助手」，然后在群里 @我 提需求。\n"
            "2. 需要我帮你新建临时群：私聊发送 `/chat new 群名 成员1 成员2`，成员可以写员工姓名或飞书 open_id。\n\n"
            "如果我已经在某个群里，要继续拉同事进来，可以在那个群里发：`/chat invite 成员1 成员2`。"
        )

    @filter.command(
        "chat",
        desc="群操作：/chat new <群名> <成员...> / /chat invite <成员...>",
    )
    async def chat_command(self, event: AstrMessageEvent):
        text = (event.message_str or "").strip()
        for prefix in ("/chat", "chat"):
            if text.startswith(prefix):
                text = text[len(prefix) :].strip()
                break
        parts = text.split(maxsplit=1)
        sub = parts[0].lower() if parts else ""
        rest = parts[1] if len(parts) > 1 else ""

        if sub == "invite":
            await self._handle_invite(event, rest)
            return

        if sub != "new":
            self._reply(
                event,
                "用法：\n"
                "  /chat new <群名> <成员1> <成员2> ...  在飞书新建临时群\n"
                "  /chat invite <成员1> <成员2> ...      把人拉进当前群\n"
                "成员可写：飞书 open_id (ou_xxx) / 员工名 / @员工名",
            )
            return

        if self.creator is None:
            self._reply(
                event,
                "⚠️ 建群功能未启用：缺 `data/feishu_whitelist.yaml` 里的 "
                "`feishu.app_id` + `feishu.app_secret`，并在飞书后台勾 "
                "`im:chat` + `im:chat.member` 权限。",
            )
            return

        # 仅允许飞书发起（QQ 无法建飞书群）
        platform = event.get_platform_id() or ""
        # 飞书的 platform_id 通常含 "lark"
        if "lark" not in platform.lower() and "feishu" not in platform.lower():
            self._reply(event, "⚠️ 此命令只能在飞书里使用。")
            return

        # 解析 群名 + 成员列表
        tokens = rest.split()
        if len(tokens) < 1:
            self._reply(event, "用法：/chat new <群名> <成员1> <成员2> ...")
            return
        chat_name = tokens[0]
        member_tokens = tokens[1:]

        # 转 open_id
        member_open_ids, unresolved = await self._resolve_members(member_tokens)

        # owner = 发送者
        owner_open_id = event.get_sender_id() or ""
        if not owner_open_id:
            self._reply(event, "⚠️ 无法识别你的飞书身份。")
            return
        # owner 不该出现在成员列表里（飞书 API 限制）
        member_open_ids = [m for m in member_open_ids if m != owner_open_id]

        # 调引擎建群
        req = ChatCreationRequest(
            name=chat_name,
            owner_open_id=owner_open_id,
            member_open_ids=member_open_ids,
            description=f"DC-Agent 临时群，由 {owner_open_id[:8]}... 发起",
        )
        result = await self.creator.create_group_chat(req)

        if not result.success:
            self._reply(event, f"⚠️ 建群失败：{result.error}")
            return

        lines = [
            f"✅ 已建临时群：**{chat_name}**",
            f"  • chat_id: `{result.chat_id}`",
            f"  • 邀请成员: {result.invited_count} 人",
        ]
        if result.invalid_member_ids:
            lines.append(
                f"  • 邀请失败 {len(result.invalid_member_ids)} 人："
                f"{', '.join(result.invalid_member_ids[:5])}"
            )
        if unresolved:
            lines.append(f"  • 未识别的成员 token：{', '.join(unresolved[:5])}")
        if result.chat_url:
            lines.append(f"  • 链接：{result.chat_url}")
        lines.append("")
        lines.append("_提示：在飞书 App 中打开 chat_id 进入新群继续讨论。_")
        self._reply(event, "\n".join(lines))

        logger.info(
            "[chat_creator] 建群成功 chat_id=%s owner=%s members=%d",
            result.chat_id,
            owner_open_id[:8],
            result.invited_count,
        )

    async def _handle_invite(self, event: AstrMessageEvent, rest: str) -> None:
        """/chat invite <成员...> 把人拉进当前群（chat_id 来自 event.group_id）。"""
        if self.creator is None:
            self._reply(
                event,
                "⚠️ 建群引擎未启用：缺 `data/feishu_whitelist.yaml` 凭证；"
                "需在飞书后台勾 `im:chat.member`。",
            )
            return

        platform = event.get_platform_id() or ""
        if "lark" not in platform.lower() and "feishu" not in platform.lower():
            self._reply(event, "⚠️ 此命令只能在飞书使用。")
            return

        chat_id: str | None = None
        try:
            chat_id = event.get_group_id()
        except Exception:  # noqa: BLE001
            chat_id = None
        if not chat_id:
            self._reply(event, "⚠️ 当前不是群聊（私聊无法 invite）。")
            return

        tokens = rest.split()
        if not tokens:
            self._reply(
                event,
                "用法：/chat invite <成员1> <成员2> ...（open_id / 员工名 / @员工名）",
            )
            return

        member_open_ids, unresolved = await self._resolve_members(tokens)
        if not member_open_ids:
            self._reply(
                event,
                f"⚠️ 没解析出任何 open_id；未识别的 token：{', '.join(unresolved[:8])}",
            )
            return

        ok, invalid, err = await self.creator.invite_members(chat_id, member_open_ids)
        if err:
            self._reply(event, f"⚠️ 邀请失败：{err}")
            return

        lines = [f"✅ 已向当前群拉人：{ok} 成功"]
        if invalid:
            lines.append(
                f"  • {len(invalid)} 个 open_id 未成功（已在群/退群/失效等）："
                f"{', '.join(invalid[:5])}"
            )
        if unresolved:
            lines.append(f"  • 未识别 token：{', '.join(unresolved[:5])}")
        self._reply(event, "\n".join(lines))

        logger.info(
            "[chat_creator] invite chat_id=%s ok=%d invalid=%d unresolved=%d",
            chat_id[:12] if chat_id else "?",
            ok,
            len(invalid),
            len(unresolved),
        )

    async def _resolve_members(self, tokens: list[str]) -> tuple[list[str], list[str]]:
        """token → open_id 列表。

        优先级：
        1. 已是 ou_xxx（30+ 字符且以 ou_ 开头）→ 直接用
        2. 通过 employee_directory 按 display_name 查
        3. 不行就归到 unresolved
        """
        store = getattr(self.context, "employee_store", None)
        open_ids: list[str] = []
        unresolved: list[str] = []

        for t in tokens:
            cleaned = t.lstrip("@").strip()
            if not cleaned:
                continue
            # 1) raw open_id
            if cleaned.startswith("ou_") and len(cleaned) >= 12:
                open_ids.append(cleaned)
                continue
            # 2) 按姓名查
            if store is not None:
                try:
                    emps = await store.list_employees(limit=200)
                    found = next(
                        (e for e in emps if e.display_name == cleaned),
                        None,
                    )
                    if found:
                        open_ids.append(found.open_id)
                        continue
                except Exception as exc:  # noqa: BLE001
                    logger.debug("[chat_creator] 查 employee_directory 失败：%s", exc)
            unresolved.append(t)

        # 去重，保留顺序
        seen = set()
        deduped = []
        for oid in open_ids:
            if oid not in seen:
                seen.add(oid)
                deduped.append(oid)
        return deduped, unresolved
