"""AstrBot registration entry point for the DC router plugin."""

from __future__ import annotations

import json

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import register

from .config import is_business_platform, load_config
from .dispatch import dispatch
from .middle_router_adapter import route_capability_request
from .plugin import DCRouterPlugin as _DCRouterPluginBase
from .preprocessing.media_route import try_handle_media_route
from .preprocessing.session_choice import (
    resolve_pending_session_choice_for_message,
    send_session_choice_for_event,
)

_MAIN_AGENT_ROUTING_PROMPT = """
# DC 小助手 Agent / Router 协作契约

你是负责自然语言理解、对话和任务规划的 GPT-5.6-Luna 主 Agent。Router 不负责理解自然语言。

- 普通问答、解释、闲聊和可以直接完成的短回复：你直接回答，不调用 Router。
- 文案、生图、视频、研究、文件、报价、AI 转 CDR 或 Codex 高级处理等尚未确认执行的复杂工作：调用 route_agent_decision，使用 workspace.copy / workspace.image / workspace.video / workspace.research / workspace.file / workspace.quotation / workspace.ai_cdr / workspace.codex，让 Router 推出对应 H5 工作台。
- 只有当当前对话上下文中已经有用户明确、完整的图片或视频描述，并且用户刚刚明确回复“确定生成”“确认生成”“开始生成”等执行确认时，才调用 execute.image 或 execute.video，action_force 必须是 execute。goal 必须携带从真实对话上下文恢复出的完整生成描述，不能只传“确定生成”等确认短句，也不能猜测缺失参数。Router 允许后会立即启动媒体任务，不再打开 H5 工作台。
- 用户只是点击“生成图片/生成视频”菜单、仅表达想生成但描述不完整、或上下文里没有可确认的完整描述时，必须使用 workspace.image / workspace.video，action_force 使用 prepare，不得调用 execute.*。
- 用户明确要求在聊天中委派一项边界清楚的研究、办公处理或综合分析时：先调用 route_agent_decision，分别使用 delegate.research / delegate.office / delegate.analysis，action_force 必须是 execute。只有 Router 返回 allowed=true 和 next_tool 后，才允许调用对应 transfer_to_* 子代理。
- 当问题涉及公司、客户、项目、人员、部门、负责人、SOP、报价、历史方案或管理决策时，必须先使用当前上下文中已注入的公司记忆；证据不足且具备工具权限时，先调用 search_obsidian_vault，再用 read_obsidian_note 读取最相关笔记，不得仅凭聊天印象回答内部事实。
- 公司知识优先级是：当前对话明确事实 > 已批准的 Obsidian 治理记忆 > 标明状态的项目明细和原始资料。原始资料即使与 approved 记忆同时出现，只要 review_status=need_review，就仍然属于待确认材料。
- 公司知识回答必须区分“已确认事实”“资料推断”“暂未确认”，并在相关结论后给出来源路径。不得编造负责人、客户态度、项目状态、数字、日期或管理层观点。
- 如果资料存在版本冲突、负责人冲突或时间不一致，列出冲突及各自日期和来源路径，不自行选择对自己回答最方便的版本；涉及当前有效状态时优先请求人工确认。
- 不得直接选择 provider、后台实现、Harness 队列或任意工具名；这些由中置 Router 的能力目录决定。
- 信息不足时先用自然语言问一个必要问题，不猜参数、不启动任务。
- H5 工作台已经打开时，只需简短告诉用户下一步，不重复输出表单内容。
""".strip()


@register(
    "dc_router",
    "dc_agent",
    "DC 中间路由 · Agent、菜单与卡片共用的确定性能力派发层",
    "1.0.0",
)
class DCRouterPlugin(_DCRouterPluginBase):
    @filter.event_message_type(
        EventMessageType.GROUP_MESSAGE | EventMessageType.PRIVATE_MESSAGE,
        priority=210,
    )
    async def route_internal_workbench_submission(
        self,
        event: AstrMessageEvent,
    ) -> None:
        """Route trusted H5 saves before generic card-action plugins.

        Args:
            event: Internally created workbench submission event.
        """
        if event.get_extra("assistant_workbench_auto_submit") is not True or not str(
            event.message_str or ""
        ).startswith("__card_action__:"):
            return
        cfg = load_config()
        try:
            result = await dispatch(self.context, event, cfg)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[dc_router] internal workbench dispatch failed: %s", exc)
            return
        if not result.handled:
            event.set_extra("dc_internal_workbench_pre_routed", True)
            return
        logger.debug(
            "[dc_router] internal workbench handled by source=%s intent=%s",
            result.source,
            result.decision_intent or "-",
        )

    @filter.event_message_type(
        EventMessageType.GROUP_MESSAGE | EventMessageType.PRIVATE_MESSAGE,
        priority=15,
    )
    async def route(self, event: AstrMessageEvent) -> None:
        """Run access adapters and the legacy compatibility boundary.

        Args:
            event: Incoming AstrBot message event.
        """

        if event.get_extra("dc_internal_workbench_pre_routed") is True:
            return
        cfg = load_config()
        try:
            await resolve_pending_session_choice_for_message(self.context, event)
            result = await dispatch(self.context, event, cfg)
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "[dc_router] dispatch 异常，消息放行让默认 pipeline 处理: %s", exc
            )
            return
        if not result.handled:
            return
        logger.debug(
            "[dc_router] handled by source=%s intent=%s provider=%s",
            result.source,
            result.decision_intent or "-",
            result.decision_provider or "-",
        )

    @filter.on_llm_request(priority=20)
    async def inject_main_agent_contract(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> None:
        """Inject the Agent/Router Interface only on the business assistant.

        Args:
            event: Current AstrBot message event.
            req: Provider request about to enter the main Agent.
        """

        cfg = load_config()
        if not cfg.uses_middle_router or not is_business_platform(
            event.get_platform_id() or ""
        ):
            return
        req.system_prompt = (
            f"{req.system_prompt.rstrip()}\n\n{_MAIN_AGENT_ROUTING_PROMPT}"
            if req.system_prompt
            else _MAIN_AGENT_ROUTING_PROMPT
        )
        try:
            event.set_extra("dc_middle_router_enforce_handoffs", True)
        except Exception:  # noqa: BLE001
            pass

    @filter.llm_tool(name="route_agent_decision")
    async def route_agent_decision(
        self,
        event: AstrMessageEvent,
        capability_id: str,
        goal: str,
        confidence: float = 0.8,
        action_force: str = "prepare",
    ) -> str:
        """把主 Agent 的结构化任务决策交给唯一中置 Router。

        Args:
            capability_id(string): 能力 ID。未确认或信息不足的复杂工作使用 workspace.copy/image/video/research/file/quotation/ai_cdr/codex；已有完整上下文且用户明确确认生成时使用 execute.image/video；聊天内委派使用 delegate.research/office/analysis。
            goal(string): 用户明确表达的完整目标，不得包含 provider、executor 或内部工具名；execute.image/video 必须携带从真实对话上下文恢复的完整生成描述，不能只传确认短句。
            confidence(number): 对任务识别的置信度，范围 0 到 1；低于能力阈值时 Router 会拒绝。
            action_force(string): prepare 表示打开 H5 准备任务；execute 用于用户明确要求的子代理委派，或在完整媒体描述之后明确确认执行的 execute.image/video。

        Returns:
            JSON 格式的确定性路由结果；H5 能力会直接推送工作台卡片，委派能力会返回唯一允许的 next_tool。
        """

        cfg = load_config()
        if not cfg.uses_middle_router or not is_business_platform(
            event.get_platform_id() or ""
        ):
            return json.dumps(
                {"allowed": False, "reason": "middle_router_not_active"},
                ensure_ascii=False,
            )
        route = route_capability_request(
            event,
            source="agent",
            capability_id=capability_id,
            goal=goal,
            confidence=confidence,
            action_force=action_force,
            trusted=False,
        )
        payload = {
            "allowed": route.allowed,
            "request_id": route.request_id,
            "capability_id": route.capability_id,
            "target": route.target,
            "depth": route.depth,
            "reason": route.reason,
        }
        if not route.allowed:
            return json.dumps(payload, ensure_ascii=False)
        if route.target == "h5_workbench":
            from .preprocessing.assistant_workbench import _send_task_workspace

            sent = await _send_task_workspace(
                self.context,
                event,
                task_type=route.task_type,
            )
            payload["workspace_opened"] = sent
            if not sent:
                payload["reason"] = "workspace_delivery_failed"
            return json.dumps(payload, ensure_ascii=False)
        if route.target == "astrbot_subagent":
            try:
                event.set_extra("dc_middle_router_approved_handoff", route.executor)
            except Exception:  # noqa: BLE001
                pass
            payload["next_tool"] = route.executor
            return json.dumps(payload, ensure_ascii=False)
        if route.executor == "media_route":
            started = await try_handle_media_route(
                self.context,
                event,
                goal,
                capability_id=route.capability_id,
                parameters={},
            )
            payload["execution_started"] = started
            if not started:
                payload["reason"] = "media_execution_not_started"
            return json.dumps(payload, ensure_ascii=False)
        return json.dumps(payload, ensure_ascii=False)

    @filter.after_message_sent()
    async def maybe_send_session_choice(self, event: AstrMessageEvent) -> None:
        """Send the post-result conversation choice after successful delivery.

        Args:
            event: AstrBot event whose normal result has already been sent.
        """
        try:
            await send_session_choice_for_event(self.context, event)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[dc_router] post-result session choice failed: %s", exc)
