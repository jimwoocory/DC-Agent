"""W0 Harness `/task` CLI Star 插件（重装移植版）。

旧版在 ``astrbot/builtin_stars/builtin_commands/commands/harness.py``，重装后
搬到 Star plugin，零核心修改。

子命令：
  /task new <title>                普通任务
  /task intake <kind> <brief>      Workflow 任务（marketing_plan / content_delivery / project_followup / approval_request）
  /task ls                          列出当前会话任务
  /task show <id>                   任务详情
  /task start <id> [note]           标记进行中
  /task done <id> [summary]         完成
  /task approve <id> [note]         审批通过
  /task reject <id> <note>          审批拒绝

依赖：
- hermes_bridge plugin 在 initialize 阶段把 ``harness_engine`` / ``harness_store``
  装到了 context；本插件在命令运行时（不是 init）懒读，因此加载顺序无关紧要。
"""

from __future__ import annotations

from dc_engines.employee_directory import requester_meta_from_event
from dc_engines.harness import (
    HarnessTaskCreateRequest,
    create_workflow_request,
)

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.star import Context, Star, register

_VALID_WORKFLOW_KINDS: tuple[str, ...] = (
    "marketing_plan",
    "content_delivery",
    "project_followup",
    "approval_request",
)


@register(
    "task_cli_plugin",
    "dc_agent",
    "Harness 任务 CLI（W0 重装移植版），/task new|intake|ls|show|start|done|approve|reject",
    "1.0.0",
)
class TaskCliPlugin(Star):
    def __init__(self, context: Context) -> None:
        super().__init__(context)

    # --------------------------- helpers ---------------------------

    def _reply(self, event: AstrMessageEvent, text: str) -> None:
        event.set_result(MessageEventResult().message(text).use_t2i(False))

    async def _conv_id(self, event: AstrMessageEvent) -> str:
        conv_mgr = self.context.conversation_manager
        umo = event.unified_msg_origin
        cid = await conv_mgr.get_curr_conversation_id(umo)
        if cid:
            return cid
        return await conv_mgr.new_conversation(umo, event.get_platform_id())

    async def _get_task_for_current_conv(self, event: AstrMessageEvent, task_id: str):
        store = getattr(self.context, "harness_store", None)
        if store is None:
            self._reply(event, "Harness 存储未初始化（hermes_bridge plugin 未加载？）")
            return None
        task = await store.get_task(task_id.strip())
        if task is None:
            self._reply(event, "未找到该任务。")
            return None
        conv_id = await self._conv_id(event)
        if task.conversation_id != conv_id:
            self._reply(event, "该任务不属于当前会话，无法操作。")
            return None
        return task

    async def _maybe_attach_to_case(self, event: AstrMessageEvent, task) -> None:
        """新建 task 后软挂当前 active case（W0 2A-0 桥接点）。"""
        case_engine = getattr(self.context, "case_engine", None)
        if case_engine is None:
            return
        try:
            case = await case_engine.get_current_case_for_session(
                event.unified_msg_origin
            )
            if case is None:
                ensure_case = getattr(self.context, "ai_inbox_ensure_case", None)
                if ensure_case is not None:
                    case_id = await ensure_case(
                        event,
                        category="task",
                        text=event.message_str or "",
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
                        source="task_cli_plugin",
                    )
                logger.debug(
                    "[task_cli] task %s 自动挂到 case %s",
                    task.task_id[:8],
                    case.case_id[:8],
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[task_cli] case 软挂失败（不阻断）：%s", exc)

    # --------------------------- /task 主入口 ---------------------------

    @filter.command(
        "task",
        desc="Harness 任务 CLI: /task new|intake|ls|show|start|done|approve|reject",
    )
    async def task_command(self, event: AstrMessageEvent):
        text = (event.message_str or "").strip()
        if text.startswith("/task"):
            text = text[len("/task") :].strip()
        elif text.startswith("task"):
            text = text[len("task") :].strip()
        parts = text.split(maxsplit=1)
        sub = parts[0].lower() if parts else ""
        rest = parts[1] if len(parts) > 1 else ""

        if sub == "new":
            await self._task_new(event, rest)
        elif sub == "intake":
            await self._task_intake(event, rest)
        elif sub == "ls":
            await self._task_ls(event)
        elif sub == "show":
            await self._task_show(event, rest)
        elif sub == "start":
            await self._task_start(event, rest)
        elif sub == "done":
            await self._task_done(event, rest)
        elif sub == "approve":
            await self._task_approve(event, rest)
        elif sub == "reject":
            await self._task_reject(event, rest)
        else:
            self._reply(
                event,
                "用法：\n"
                "  /task new <标题>\n"
                "  /task intake <workflow_kind> <简述>\n"
                "    workflow_kind: " + " | ".join(_VALID_WORKFLOW_KINDS) + "\n"
                "  /task ls\n"
                "  /task show <task_id>\n"
                "  /task start <task_id> [备注]\n"
                "  /task done <task_id> [总结]\n"
                "  /task approve <task_id> [备注]\n"
                "  /task reject <task_id> <理由>",
            )

    # --------------------------- subcommand impls ---------------------------

    async def _task_new(self, event: AstrMessageEvent, title: str) -> None:
        title = title.strip()
        if not title:
            self._reply(event, "请输入任务标题。用法: /task new <标题>")
            return
        engine = getattr(self.context, "harness_engine", None)
        if engine is None:
            self._reply(event, "Harness 引擎未初始化。")
            return
        conv_id = await self._conv_id(event)
        requester_meta = await requester_meta_from_event(self.context, event)
        task = await engine.create_task(
            HarnessTaskCreateRequest(
                title=title,
                conversation_id=conv_id,
                platform_id=event.get_platform_id(),
                session_id=event.unified_msg_origin,
                domain="general",
                payload={
                    "source": "task_cli_plugin",
                    "message_text": event.message_str,
                    "auto_complete_on_response": False,
                    **requester_meta,
                },
            )
        )
        event.set_extra("task_cli_task_id", task.task_id)
        await self._maybe_attach_to_case(event, task)
        self._reply(
            event,
            "已创建 Harness 任务：\n"
            f"- task_id: {task.task_id}\n"
            f"- title: {task.title}\n"
            f"- status: {task.status}",
        )

    async def _task_intake(self, event: AstrMessageEvent, rest: str) -> None:
        parts = rest.strip().split(maxsplit=1)
        if len(parts) < 2:
            self._reply(
                event,
                "用法: /task intake <workflow_kind> <简述>\n"
                "  workflow_kind: " + " | ".join(_VALID_WORKFLOW_KINDS),
            )
            return
        kind, brief = parts[0].strip(), parts[1].strip()
        if kind not in _VALID_WORKFLOW_KINDS:
            self._reply(
                event,
                f"workflow_kind 无效。可选: {' | '.join(_VALID_WORKFLOW_KINDS)}",
            )
            return
        if not brief:
            self._reply(event, "请输入任务简述。")
            return
        engine = getattr(self.context, "harness_engine", None)
        if engine is None:
            self._reply(event, "Harness 引擎未初始化。")
            return
        conv_id = await self._conv_id(event)
        req = create_workflow_request(
            workflow_kind=kind,  # type: ignore[arg-type]
            brief=brief,
            conversation_id=conv_id,
            platform_id=event.get_platform_id(),
            session_id=event.unified_msg_origin,
            source="workflow_intake",
            message_text=event.message_str,
        )
        req.payload.update(await requester_meta_from_event(self.context, event))
        req.payload["auto_complete_on_response"] = False
        task = await engine.create_task(req)
        event.set_extra("task_cli_task_id", task.task_id)
        await self._maybe_attach_to_case(event, task)
        self._reply(
            event,
            "已创建 Workflow Harness 任务：\n"
            f"- task_id: {task.task_id}\n"
            f"- title: {task.title}\n"
            f"- workflow_kind: {task.payload.get('workflow_kind')}\n\n"
            "请描述你的具体需求或背景信息，我来帮你分析并制定方案。",
        )

    async def _task_ls(self, event: AstrMessageEvent) -> None:
        store = getattr(self.context, "harness_store", None)
        if store is None:
            self._reply(event, "Harness 存储未初始化。")
            return
        conv_id = await self._conv_id(event)
        tasks = await store.list_tasks_for_conversation(conv_id, limit=10)
        if not tasks:
            self._reply(event, "当前会话还没有 Harness 任务。")
            return
        lines = ["当前会话 Harness 任务："]
        for task in tasks:
            lines.append(f"- {task.task_id[:8]} | {task.status} | {task.title}")
        self._reply(event, "\n".join(lines))

    async def _task_show(self, event: AstrMessageEvent, task_id: str) -> None:
        if not task_id.strip():
            self._reply(event, "用法: /task show <task_id>")
            return
        store = getattr(self.context, "harness_store", None)
        if store is None:
            self._reply(event, "Harness 存储未初始化。")
            return
        task = await store.get_task(task_id.strip())
        if task is None:
            self._reply(event, "未找到该任务。")
            return
        events = await store.list_events(task.task_id)
        reviews = await store.list_reviews(task.task_id)
        lines = [
            "Harness 任务详情：",
            f"- task_id: {task.task_id}",
            f"- title: {task.title}",
            f"- status: {task.status}",
            f"- domain: {task.domain}",
            f"- conversation_id: {task.conversation_id}",
            f"- events: {len(events)}",
            f"- reviews: {len(reviews)}",
        ]
        workflow_kind = task.payload.get("workflow_kind")
        if workflow_kind:
            lines.append(f"- workflow_kind: {workflow_kind}")
        result = task.result or {}
        summary = result.get("summary")
        if summary:
            lines.append(f"- summary: {summary[:200]}")
        self._reply(event, "\n".join(lines))

    async def _task_start(self, event: AstrMessageEvent, rest: str) -> None:
        parts = rest.strip().split(maxsplit=1)
        task_id = parts[0] if parts else ""
        note = parts[1] if len(parts) > 1 else ""
        if not task_id:
            self._reply(event, "用法: /task start <task_id> [备注]")
            return
        engine = getattr(self.context, "harness_engine", None)
        if engine is None:
            self._reply(event, "Harness 引擎未初始化。")
            return
        task = await self._get_task_for_current_conv(event, task_id)
        if task is None:
            return
        updated = await engine.mark_in_progress(task.task_id, note=note.strip() or None)
        self._reply(
            event,
            f"任务已开始：{updated.task_id[:8]} | {updated.status} | {updated.title}",
        )

    async def _task_done(self, event: AstrMessageEvent, rest: str) -> None:
        parts = rest.strip().split(maxsplit=1)
        task_id = parts[0] if parts else ""
        summary = parts[1] if len(parts) > 1 else ""
        if not task_id:
            self._reply(event, "用法: /task done <task_id> [总结]")
            return
        engine = getattr(self.context, "harness_engine", None)
        if engine is None:
            self._reply(event, "Harness 引擎未初始化。")
            return
        task = await self._get_task_for_current_conv(event, task_id)
        if task is None:
            return
        result: dict = {"source": "task_cli_plugin/done"}
        if summary.strip():
            result["summary"] = summary.strip()[:200]
        updated = await engine.complete_task(task.task_id, result=result)
        inbox_store = getattr(self.context, "ai_inbox_store", None)
        if inbox_store is not None:
            try:
                item = await inbox_store.find_by_task_id(task.task_id)
                if item is not None:
                    await inbox_store.update_item(
                        item.item_id,
                        status="closed",
                        event_type="task_closed",
                        event_payload={"source": "task_cli_plugin/done"},
                    )
            except Exception as exc:  # noqa: BLE001
                logger.debug("[task_cli] inbox close skipped: %s", exc)
        self._reply(
            event,
            f"任务已完成：{updated.task_id[:8]} | {updated.status} | {updated.title}",
        )

    async def _task_approve(self, event: AstrMessageEvent, rest: str) -> None:
        parts = rest.strip().split(maxsplit=1)
        task_id = parts[0] if parts else ""
        note = parts[1] if len(parts) > 1 else ""
        if not task_id:
            self._reply(event, "用法: /task approve <task_id> [备注]")
            return
        engine = getattr(self.context, "harness_engine", None)
        if engine is None:
            self._reply(event, "Harness 引擎未初始化。")
            return
        task = await self._get_task_for_current_conv(event, task_id)
        if task is None:
            return
        review = await engine.approve_task(
            task.task_id,
            reviewer_id=event.get_sender_id() or "?",
            note=note.strip(),
        )
        self._reply(
            event, f"任务已审批通过：{task.task_id[:8]} | review {review.review_id[:8]}"
        )

    async def _task_reject(self, event: AstrMessageEvent, rest: str) -> None:
        parts = rest.strip().split(maxsplit=1)
        task_id = parts[0] if parts else ""
        note = parts[1] if len(parts) > 1 else ""
        if not task_id:
            self._reply(event, "用法: /task reject <task_id> <理由>")
            return
        if not note.strip():
            self._reply(event, "请填写拒绝理由。用法: /task reject <task_id> <理由>")
            return
        engine = getattr(self.context, "harness_engine", None)
        if engine is None:
            self._reply(event, "Harness 引擎未初始化。")
            return
        task = await self._get_task_for_current_conv(event, task_id)
        if task is None:
            return
        review = await engine.reject_task(
            task.task_id,
            reviewer_id=event.get_sender_id() or "?",
            note=note.strip(),
        )
        self._reply(
            event,
            f"任务已驳回：{task.task_id[:8]} | review {review.review_id[:8]}"
            f"\n理由：{note.strip()[:80]}",
        )
