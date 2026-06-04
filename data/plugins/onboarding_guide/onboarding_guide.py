"""
新员工引导插件

首次对话时通过 ABC 选择引导员工完成角色配置，自动绑定对应人格。
完成后进入正常模式，不再拦截消息。
"""

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.core import sp

_WELCOME = """\
你好呀～我是 **巅池-Agent 小助手**，公司给所有同事配的智能伙伴 🤝

为了更懂你的工作场景，方便先告诉我 **你主要做哪类工作**？回一个字母就行：

  📣 **A** — 推广 / 运营 / 内容（市场部 / 品宣部 / 影视部）
  📋 **B** — 项目管理 / 跟进（策略部 / 执行运营 / 总经办）
  ⚙️ **C** — 技术 / 运维（IT / 系统支持）

回答完我会切到对应的"专家模式"，回答更对路。\
"""

_CONFIRM = {
    "A": (
        "Biz_Assistant_Claw",
        "推广运营助手",
        "好的～切到「推广运营」模式 📣\n我最擅长：营销方案、文案、内容创意、投放策略、追热点。\n\n试试发：**帮我做一个端午社交媒体推广创意**",
    ),
    "B": (
        "Enterprise_Ops_Kernel",
        "项目跟进助手",
        "好的～切到「项目跟进」模式 📋\n我最擅长：项目排期、跟进汇报、审批起草、跨部门协同、周报月报。\n\n试试发：**帮我整理一份本周项目进度跟进表**",
    ),
    "C": (
        "DevOps_Console",
        "技术运维助手",
        "好的～切到「技术运维」模式 ⚙️\n我最擅长：系统排障、运维流程、技术方案、代码审查、问题分析。\n\n试试发：**帮我检查一下今天的服务状态**",
    ),
}

_INVALID = (
    "回 A、B 或 C 任意一个字母就行～如果不确定选哪个，按你**最主要的工作内容**选。"
)

_SP_KEY_STATE = "onboarding_state"
_SP_KEY_SESSION = "session_service_config"
_STATE_SELECTING = "selecting_role"
_STATE_DONE = "completed"


@register(
    "onboarding_guide",
    "dc_agent",
    "新员工引导：首次对话 ABC 选角色 → 自动绑定对应 persona",
    "1.0.0",
)
class OnboardingGuidePlugin(Star):
    def __init__(self, context: Context) -> None:
        super().__init__(context)

    async def on_message(self, event: AstrMessageEvent) -> None:
        umo = event.unified_msg_origin

        state = await sp.get_async(
            scope="umo",
            scope_id=umo,
            key=_SP_KEY_STATE,
            default=None,
        )

        # 已完成引导，让正常 pipeline 处理
        if state == _STATE_DONE:
            return

        # 群聊里不主动弹引导菜单 —— N 个员工同时收到 ABC 选项会刷屏。
        # 群里员工想配置自己的角色 → 私聊机器人触发引导即可。
        if ":GroupMessage:" in umo:
            await sp.put_async(
                scope="umo",
                scope_id=umo,
                key=_SP_KEY_STATE,
                value=_STATE_DONE,
            )
            return

        text = (event.message_str or "").strip().upper()

        if state is None:
            # 第一次对话：进入选择角色步骤
            await sp.put_async(
                scope="umo",
                scope_id=umo,
                key=_SP_KEY_STATE,
                value=_STATE_SELECTING,
            )
            self._reply(event, _WELCOME)
            return

        if state == _STATE_SELECTING:
            if text in _CONFIRM:
                persona_id, role_name, hint = _CONFIRM[text]
                await self._bind_persona(umo, persona_id)
                await sp.put_async(
                    scope="umo",
                    scope_id=umo,
                    key=_SP_KEY_STATE,
                    value=_STATE_DONE,
                )
                msg = f"✅ {hint}"
                logger.info(
                    "[Onboarding] %s 完成引导，角色=%s persona=%s",
                    umo,
                    role_name,
                    persona_id,
                )
                self._reply(event, msg)
            else:
                self._reply(event, _INVALID)

    # ── helpers ──────────────────────────────────────────────────────────────

    def _reply(self, event: AstrMessageEvent, text: str) -> None:
        event.set_result(MessageEventResult().message(text).use_t2i(False))
        # 阻止后续 LLM 处理这条消息
        event.should_call_llm(True)

    async def _bind_persona(self, umo: str, persona_id: str) -> None:
        session_config = (
            await sp.get_async(
                scope="umo",
                scope_id=umo,
                key=_SP_KEY_SESSION,
                default={},
            )
            or {}
        )
        session_config["persona_id"] = persona_id
        await sp.put_async(
            scope="umo",
            scope_id=umo,
            key=_SP_KEY_SESSION,
            value=session_config,
        )
