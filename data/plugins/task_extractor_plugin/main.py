"""W2 / 2A-2 任务提取提醒 Star 插件（员工问卷 91% 强需求）。

入口 A：@DC-Agent + 关键词 → 抽出最近聊天里的任务，每个落 Harness task，挂当前 case
入口 B：/tasks 命令 → 查询当前 case 的所有任务

低打扰：群里必须 @ + 关键词；私聊关键词即可。
"""

from __future__ import annotations

from datetime import datetime, timezone

from dc_engines.employee_directory import requester_meta_from_event
from dc_engines.group_summary import parse_time_range
from dc_engines.group_summary.history_fetcher import (
    UnsupportedPlatformError,
    fetch_group_messages,
)
from dc_engines.harness import HarnessTaskCreateRequest
from dc_engines.task_extractor import extract_tasks_from_messages

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.star import Context, Star, register

# 关键词触发器
_TRIGGER_KEYWORDS: tuple[str, ...] = (
    "提醒",
    "待办",
    "汇总待办",
    "抽任务",
    "抽待办",
    "任务列表",
    "我的待办",
    "todo",
)


@register(
    "task_extractor_plugin",
    "dc_agent",
    "任务提取提醒（W2 / 2A-2，员工问卷 91% 强需求）",
    "1.0.0",
)
class TaskExtractorPlugin(Star):
    def __init__(self, context: Context) -> None:
        super().__init__(context)

    def _reply(self, event: AstrMessageEvent, text: str, stop: bool = True) -> None:
        result = MessageEventResult().message(text).use_t2i(False)
        if stop:
            result.stop_event()
        event.set_result(result)

    # --------------------------- /tasks 查询命令 ---------------------------

    @filter.command("tasks", desc="查询当前 case 的所有 Harness 任务（W2 / 2A-2）")
    async def tasks_command(self, event: AstrMessageEvent):
        store = getattr(self.context, "harness_store", None)
        case_engine = getattr(self.context, "case_engine", None)
        if store is None:
            self._reply(event, "Harness 存储未初始化。")
            return

        text = (event.message_str or "").strip()
        if text.startswith("/tasks"):
            text = text[len("/tasks") :].strip()
        elif text.startswith("tasks"):
            text = text[len("tasks") :].strip()
        show_all = text.lower() in ("all", "*")
        filter_user = ""
        if text.startswith("@"):
            filter_user = text.lstrip("@").strip().lower()

        # 1) 优先看当前 case 的 task；2) 没 case 则看当前会话所有 task
        tasks: list = []
        case_label = ""
        if case_engine is not None:
            try:
                case = await case_engine.get_current_case_for_session(
                    event.unified_msg_origin
                )
                if case is not None:
                    case_label = f"Case {case.case_id[:8]} · {case.name}"
                    for tid in case.task_ids:
                        t = await store.get_task(tid)
                        if t is not None:
                            tasks.append(t)
            except Exception as exc:  # noqa: BLE001
                logger.debug("[task_extractor] 取 case task 失败：%s", exc)

        if not tasks:
            try:
                tasks = await store.list_tasks_for_session(
                    event.unified_msg_origin, limit=20
                )
            except Exception:  # noqa: BLE001
                tasks = []

        # 过滤
        if not show_all:
            tasks = [
                t
                for t in tasks
                if t.status in ("pending", "in_progress", "review_required", "blocked")
            ]
        if filter_user:
            tasks = [
                t
                for t in tasks
                if filter_user in (str(t.payload.get("assignee_hint", "")).lower())
                or filter_user in (str(t.payload.get("assignee_user_id", "")).lower())
            ]

        if not tasks:
            self._reply(event, "暂无待办任务。", stop=True)
            return

        # 按 deadline 升序，无 deadline 放末尾
        def _deadline_key(t):
            dl = t.payload.get("deadline")
            if dl:
                try:
                    return (0, datetime.fromisoformat(str(dl)))
                except Exception:
                    return (1, datetime.max)
            return (1, datetime.max)

        tasks.sort(key=_deadline_key)

        lines = [case_label or f"当前会话任务（{len(tasks)} 条）："]
        if case_label:
            lines[0] += f"\n任务（{len(tasks)} 条）："
        for t in tasks[:20]:
            assignee = t.payload.get("assignee_hint") or "—"
            dl = t.payload.get("deadline")
            dl_str = ""
            if dl:
                try:
                    dl_dt = datetime.fromisoformat(str(dl))
                    dl_str = dl_dt.strftime(" · 截止 %m-%d %H:%M")
                except Exception:
                    pass
            prio = t.payload.get("priority", "normal")
            prio_mark = "🔴" if prio == "high" else ("🟢" if prio == "low" else "·")
            lines.append(
                f"{prio_mark} [{t.status[:4]}] {t.title[:40]}  {assignee}{dl_str}  #{t.task_id[:8]}"
            )
        self._reply(event, "\n".join(lines))

    # --------------------------- 群消息：关键词触发抽取 ---------------------------

    @filter.event_message_type(
        EventMessageType.GROUP_MESSAGE | EventMessageType.PRIVATE_MESSAGE
    )
    async def on_message(self, event: AstrMessageEvent):
        text = (event.message_str or "").strip()
        if not text:
            return

        # 群里必须 @
        is_group = "GroupMessage" in (event.unified_msg_origin or "")
        if is_group and not getattr(event, "is_at_or_wake_command", False):
            return

        # 必须命中关键词
        if not any(kw in text for kw in _TRIGGER_KEYWORDS):
            return

        # 找 provider
        provider = self.context.get_using_provider(event.unified_msg_origin)
        if provider is None:
            self._reply(event, "⚠️ 当前未配置 LLM provider，无法抽任务")
            return

        # 找 platform_inst（飞书 / QQ）
        target_id = event.get_platform_id()
        platform_inst = None
        for inst in self.context.platform_manager.platform_insts:
            try:
                if inst.meta().id == target_id:
                    platform_inst = inst
                    break
            except Exception:  # noqa: BLE001
                continue
        if platform_inst is None:
            self._reply(event, f"⚠️ 未找到平台实例 {target_id!r}")
            return

        # 时间窗（沿用 group_summary 的解析）
        time_range = parse_time_range(text, now=datetime.now(timezone.utc))

        # 拉历史
        try:
            messages = await fetch_group_messages(
                platform_inst=platform_inst,
                message_history_manager=self.context.message_history_manager,
                event=event,
                time_range=time_range,
            )
        except UnsupportedPlatformError as exc:
            self._reply(event, f"⚠️ 当前平台暂不支持抽任务：{exc}")
            return

        if not messages:
            self._reply(event, f"⚠️ 在「{time_range.description}」窗口内没有可抽的消息")
            return

        # LLM 抽取
        extracted = await extract_tasks_from_messages(
            messages,
            llm_provider=provider,
            time_now=datetime.now(timezone.utc),
        )

        if not extracted:
            self._reply(
                event,
                f"在「{time_range.description}」内没抽到明确待办（{len(messages)} 条聊天已分析）。",
            )
            return

        # 落 Harness task + 软挂 case
        engine = getattr(self.context, "harness_engine", None)
        if engine is None:
            self._reply(event, "⚠️ Harness 引擎未就绪，无法落任务")
            return

        try:
            conv_id = await self.context.conversation_manager.get_curr_conversation_id(
                event.unified_msg_origin
            )
            if not conv_id:
                conv_id = await self.context.conversation_manager.new_conversation(
                    event.unified_msg_origin, event.get_platform_id()
                )
        except Exception:
            self._reply(event, "⚠️ 获取会话失败")
            return

        case_engine = getattr(self.context, "case_engine", None)
        active_case = None
        if case_engine is not None:
            try:
                active_case = await case_engine.get_current_case_for_session(
                    event.unified_msg_origin
                )
                if active_case is None:
                    ensure_case = getattr(self.context, "ai_inbox_ensure_case", None)
                    if ensure_case is not None:
                        case_id = await ensure_case(
                            event,
                            category="task",
                            text=text,
                        )
                        if case_id:
                            active_case = await case_engine.store.get_case(case_id)
            except Exception:  # noqa: BLE001
                pass

        requester_meta = await requester_meta_from_event(self.context, event)
        created_tasks: list = []
        for ex in extracted:
            payload = {
                "source": "task_extractor_plugin",
                "extracted_by": "v1.0",
                "assignee_hint": ex.assignee_hint,
                "assignee_user_id": ex.assignee_user_id,
                "deadline_raw": ex.deadline_raw,
                "deadline": ex.deadline.isoformat() if ex.deadline else None,
                "priority": ex.priority,
                "confidence": ex.confidence,
                "auto_complete_on_response": False,
            }
            payload.update(requester_meta)
            try:
                t = await engine.create_task(
                    HarnessTaskCreateRequest(
                        title=ex.description[:80],
                        conversation_id=conv_id,
                        platform_id=event.get_platform_id(),
                        session_id=event.unified_msg_origin,
                        domain="task_extract",
                        payload=payload,
                    )
                )
                created_tasks.append((t, ex))
                if active_case is not None:
                    try:
                        await case_engine.attach_task(active_case.case_id, t.task_id)
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("[task_extractor] case 挂接失败：%s", exc)
            except Exception:
                logger.warning("[task_extractor] create_task 失败", exc_info=True)

        if not created_tasks:
            self._reply(event, "⚠️ 抽到任务但落库失败，请稍后重试")
            return

        event.set_extra(
            "task_extractor_task_ids", [t.task_id for t, _ in created_tasks]
        )
        link_task = getattr(self.context, "ai_inbox_link_task", None)
        if link_task is not None:
            first_task = created_tasks[0][0]
            await link_task(
                event,
                first_task.task_id,
                status="delivered",
                case_id=active_case.case_id if active_case is not None else "",
                source="task_extractor_plugin",
            )

        # 回复格式化
        lines = [
            f"📋 抽到 {len(created_tasks)} 条待办（来自「{time_range.description}」{len(messages)} 条聊天）"
        ]
        if active_case is not None:
            lines[0] += f"，已挂到 Case {active_case.case_id[:8]}"
        for t, ex in created_tasks[:10]:
            prio_mark = (
                "🔴"
                if ex.priority == "high"
                else ("🟢" if ex.priority == "low" else "·")
            )
            assignee = ex.assignee_hint or "—"
            dl_str = ""
            if ex.deadline_raw:
                dl_str = f" · 截止 {ex.deadline_raw}"
            lines.append(
                f"{prio_mark} {t.title[:40]}  {assignee}{dl_str}  #{t.task_id[:8]}"
            )
        lines.append("")
        lines.append("用 `/tasks` 随时查 · `/task done <id>` 完成")
        self._reply(event, "\n".join(lines))

        logger.info(
            "[task_extractor] umo=%s msgs=%d extracted=%d range=%s",
            event.unified_msg_origin,
            len(messages),
            len(created_tasks),
            time_range.description,
        )
