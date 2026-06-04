"""W1 / 2A-1 项目群聊总结 Star 插件（重装移植版）。

新架构：业务逻辑全部在 ``dc_engines.group_summary``，本插件只做：
1. 注册 GROUP_MESSAGE handler，过滤 @DC-Agent + 总结关键词
2. 找 provider + platform_inst
3. 调 dc_engines 三件套（fetch / summarize / format）
4. 回群 + 软挂到当前 Case（如有）

低打扰：必须 @ + 关键词双满足；纯 @ 不抢答；任何失败给明确提示。
"""

from __future__ import annotations

from datetime import datetime, timezone

from dc_engines.group_summary import (
    fetch_group_messages,
    format_summary,
    parse_time_range,
    summarize_group_chat,
)
from dc_engines.group_summary.history_fetcher import UnsupportedPlatformError

from astrbot.api import logger
from astrbot.api.event import (
    AstrMessageEvent,
    MessageEventResult,
    filter,
)
from astrbot.api.event.filter import EventMessageType
from astrbot.api.star import Context, Star, register

# 触发关键词：必须 @DC-Agent + 命中其中之一才出总结
SUMMARY_KEYWORDS: tuple[str, ...] = (
    "群聊总结",
    "总结群",
    "汇总群",
    "总结今天",
    "总结昨天",
    "总结最近",
    "总结一下",
    "汇总待办",
    "聊天总结",
    "summary",
)


@register(
    "group_summary_plugin",
    "dc_agent",
    "项目群聊总结（W1 / 2A-1 重装移植版）",
    "1.0.0",
)
class GroupSummaryPlugin(Star):
    def __init__(self, context: Context) -> None:
        super().__init__(context)

    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        # 低打扰门槛：必须 @ + 关键词
        if not getattr(event, "is_at_or_wake_command", False):
            return
        text = event.message_str or ""
        if not any(kw in text for kw in SUMMARY_KEYWORDS):
            return

        # 找 LLM provider
        provider = self.context.get_using_provider(event.unified_msg_origin)
        if provider is None:
            event.set_result(
                MessageEventResult()
                .message("⚠️ 当前未配置 LLM provider，无法做群聊总结")
                .use_t2i(False)
                .stop_event()
            )
            return

        # 找当前平台实例
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
            event.set_result(
                MessageEventResult()
                .message(f"⚠️ 未找到平台实例 {target_id!r}，无法拉取群聊历史")
                .use_t2i(False)
                .stop_event()
            )
            return

        # 解析时间范围（从消息文本抠）
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
            event.set_result(
                MessageEventResult()
                .message(f"⚠️ 当前平台暂不支持群聊总结：{exc}")
                .use_t2i(False)
                .stop_event()
            )
            return

        if not messages:
            event.set_result(
                MessageEventResult()
                .message(f"⚠️ 在「{time_range.description}」窗口内没有可总结的群聊消息")
                .use_t2i(False)
                .stop_event()
            )
            return

        # 用 employee_directory 富化 sender：raw open_id → "张三 · 业务部"。
        # 让 LLM 总结里看到的是真实姓名+部门，而不是 ou_xxx 这种字符串。
        # employee_store 不存在 / 查询失败 → 静默回退到原 sender 值。
        emp_store = getattr(self.context, "employee_store", None)
        if emp_store is not None and messages:
            try:
                emp_list = await emp_store.list_employees(limit=500)
                emp_by_id = {e.open_id: e for e in emp_list if e.open_id}
                enriched_count = 0
                for m in messages:
                    sid = m.get("sender_id") or ""
                    if not sid or sid not in emp_by_id:
                        continue
                    emp = emp_by_id[sid]
                    if not emp.display_name:
                        continue
                    # 原 sender 若就是 open_id（没真名），或为空，替换为 "姓名 · 部门"
                    original = (m.get("sender") or "").strip()
                    if original and original != sid and "·" in original:
                        continue  # 已经富化过，跳过
                    label = emp.display_name
                    if emp.department:
                        label = f"{label} · {emp.department}"
                    m["sender"] = label
                    enriched_count += 1
                if enriched_count:
                    logger.debug(
                        "[group_summary] sender 富化 %d / %d 条消息",
                        enriched_count,
                        len(messages),
                    )
            except Exception as exc:  # noqa: BLE001
                logger.debug("[group_summary] sender 富化失败：%s", exc)

        # 调 LLM 总结
        summary = await summarize_group_chat(
            messages, llm_provider=provider, time_range=time_range
        )

        # 回群
        md = format_summary(summary)
        event.set_result(MessageEventResult().message(md).use_t2i(False).stop_event())

        # Case 软挂接（失败不阻断）
        try:
            case_engine = getattr(self.context, "case_engine", None)
            if case_engine is not None:
                case = await case_engine.get_current_case_for_session(
                    event.unified_msg_origin
                )
                if case is not None:
                    await case_engine.add_deliverable(
                        case.case_id,
                        kind="group_summary",
                        path=(
                            f"in-memory://summary-{datetime.now(timezone.utc).isoformat()}"
                        ),
                        version=case.version,
                    )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[group_summary] Case soft-attach 跳过：%s", exc)

        logger.info(
            "[group_summary] 完成总结 umo=%s msg_count=%d range=%s",
            event.unified_msg_origin,
            summary.message_count,
            time_range.description,
        )
