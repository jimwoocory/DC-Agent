"""W0 Workflow 意图识别 Star 插件（重装移植版）。

旧版逻辑：router_config.yaml 4 个 workflow_kind 关键词规则 → router_stage._handle_task_intent
→ engine.create_task。
新架构：纯 Star plugin，监听群消息，关键词命中即建 Harness workflow task + 软挂当前 case，
**不拦截事件**，LLM 继续答（保留旧版的"轻 agent + 重 fallback"原则）。

触发示例（飞书群 / QQ 群）：
  "下个季度的营销计划要赶紧搞起来"     → marketing_plan
  "内容交付清单麻烦整理"                 → content_delivery
  "今天的项目跟进汇报"                   → project_followup
  "发起审批：差旅报销"                    → approval_request

低打扰：必须 @DC-Agent + 关键词双满足（私聊无门槛），避免误触发普通讨论。
"""

from __future__ import annotations

from dc_engines.harness import create_workflow_request

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.star import Context, Star, register

# 关键词 → workflow_kind 映射（迁自旧 router_config.yaml）
_KEYWORDS: dict[str, tuple[str, ...]] = {
    "marketing_plan": ("营销计划", "推广计划", "营销方案", "渠道规划"),
    "content_delivery": ("内容交付", "交付物", "素材交付", "deliverables"),
    "project_followup": ("项目跟进", "follow-up", "followup", "项目跟踪"),
    "approval_request": ("审批请求", "发起审批", "需要审批", "approval request"),
}
_INTERNAL_CONTEXT_MARKERS: tuple[str, ...] = ("<dc_agent_memory_context>",)


@register(
    "workflow_intent_plugin",
    "dc_agent",
    "Workflow 关键词识别 → 自动建 Harness task + 软挂 Case（W0 重装移植版）",
    "1.0.0",
)
class WorkflowIntentPlugin(Star):
    def __init__(self, context: Context) -> None:
        super().__init__(context)

    @filter.event_message_type(
        EventMessageType.GROUP_MESSAGE | EventMessageType.PRIVATE_MESSAGE
    )
    async def on_message(self, event: AstrMessageEvent):
        text = (event.message_str or "").strip()
        if not text:
            return
        if any(marker in text for marker in _INTERNAL_CONTEXT_MARKERS):
            return

        # 群里必须 @DC-Agent（私聊无门槛）
        is_group = "GroupMessage" in (event.unified_msg_origin or "") or "Group" in str(
            getattr(event.message_obj, "type", "")
        )
        if is_group and not getattr(event, "is_at_or_wake_command", False):
            return

        # 关键词匹配（first hit wins）
        matched_kind: str | None = None
        matched_kw: str | None = None
        for kind, kws in _KEYWORDS.items():
            for kw in kws:
                if kw in text:
                    matched_kind = kind
                    matched_kw = kw
                    break
            if matched_kind:
                break
        if not matched_kind:
            return

        # 防重复：同会话短时间内已建过同 kind 的 task 就跳过
        engine = getattr(self.context, "harness_engine", None)
        store = getattr(self.context, "harness_store", None)
        if engine is None or store is None:
            return  # Harness 没就绪 → 静默跳过

        try:
            conv_id = await self.context.conversation_manager.get_curr_conversation_id(
                event.unified_msg_origin
            )
            if not conv_id:
                conv_id = await self.context.conversation_manager.new_conversation(
                    event.unified_msg_origin, event.get_platform_id()
                )
            recent = await store.list_tasks_for_conversation(conv_id, limit=5)
            for t in recent:
                if t.payload.get("workflow_kind") == matched_kind and t.status in (
                    "pending",
                    "in_progress",
                ):
                    logger.debug(
                        "[workflow_intent] 跳过：%s 已有活跃 task %s",
                        matched_kind,
                        t.task_id[:8],
                    )
                    return
        except Exception as exc:  # noqa: BLE001
            logger.debug("[workflow_intent] 防重复查询失败：%s", exc)
            return

        # 查请求人画像（找不到就 fallback 到 raw open_id，不阻断建 task）
        requester_meta: dict = {}
        emp_store = getattr(self.context, "employee_store", None)
        sender_id = ""
        try:
            sender_id = str(event.get_sender_id() or "").strip()
        except Exception:  # noqa: BLE001
            sender_id = ""
        if emp_store is not None and sender_id:
            try:
                emp = await emp_store.get_employee(sender_id)
                if emp is not None:
                    requester_meta = {
                        "requester_open_id": emp.open_id,
                        "requester_display_name": emp.display_name or "",
                        "requester_department": emp.department or "",
                        "requester_role": emp.role or "",
                    }
            except Exception as exc:  # noqa: BLE001
                logger.debug("[workflow_intent] 查请求人失败：%s", exc)
        if not requester_meta and sender_id:
            requester_meta = {"requester_open_id": sender_id}

        # 创建 task
        try:
            req = create_workflow_request(
                workflow_kind=matched_kind,  # type: ignore[arg-type]
                brief=text[:200],
                conversation_id=conv_id,
                platform_id=event.get_platform_id(),
                session_id=event.unified_msg_origin,
                source="workflow_intent_plugin",
                message_text=text,
            )
            # 请求人画像写进 payload（payload 是 dict，加字段不破坏 schema）
            if requester_meta:
                req.payload.update(requester_meta)
            req.payload["auto_complete_on_response"] = True
            task = await engine.create_task(req)
            event.set_extra("workflow_intent_task_id", task.task_id)
            link_task = getattr(self.context, "ai_inbox_link_task", None)
            if link_task is not None:
                await link_task(
                    event,
                    task.task_id,
                    source="workflow_intent_plugin",
                )
            requester_label = requester_meta.get("requester_display_name") or (
                sender_id[:12] + "..." if sender_id else "anon"
            )
            logger.info(
                "[workflow_intent] 自动建 Harness task umo=%s kind=%s kw=%s requester=%s task=%s",
                event.unified_msg_origin,
                matched_kind,
                matched_kw,
                requester_label,
                task.task_id[:8],
            )
        except Exception:
            logger.warning("[workflow_intent] 创建 task 失败", exc_info=True)
            return

        # 软挂当前 active case
        try:
            case_engine = getattr(self.context, "case_engine", None)
            if case_engine is not None:
                case = await case_engine.get_current_case_for_session(
                    event.unified_msg_origin
                )
                if case is None:
                    ensure_case = getattr(self.context, "ai_inbox_ensure_case", None)
                    if ensure_case is not None:
                        case_id = await ensure_case(
                            event,
                            category="request",
                            text=text,
                            task_id=task.task_id,
                        )
                        if case_id:
                            case = await case_engine.store.get_case(case_id)
                if case is not None:
                    await case_engine.attach_task(case.case_id, task.task_id)
                    link_task = getattr(self.context, "ai_inbox_link_task", None)
                    if link_task is not None:
                        await link_task(
                            event,
                            task.task_id,
                            case_id=case.case_id,
                            source="workflow_intent_plugin",
                        )
                    logger.debug(
                        "[workflow_intent] task %s 自动挂到 case %s",
                        task.task_id[:8],
                        case.case_id[:8],
                    )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[workflow_intent] case 软挂失败：%s", exc)

        # **不拦截事件** —— 让 LLM 继续正常响应用户
        # （旧版 router_stage._handle_task_intent 也是 return False 不打断）
