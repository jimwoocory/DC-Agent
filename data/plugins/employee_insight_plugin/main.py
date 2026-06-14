"""员工需求洞察闭环插件。

员工侧只处理飞书私聊：记录真实任务、卡点、暂停/退出意图，并把结构化
session/event 写入 employee insight 引擎。主动触达调度与真实发送由后续
受控任务调用，不在消息处理器里做群发副作用。
"""

from __future__ import annotations

import uuid
from pathlib import Path

from dc_engines.employee_insight_loop import (
    EmployeeInsightSession,
    EmployeeInsightSessionStatus,
    EmployeeInsightStore,
    InsightEvent,
)

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.star import Context, Star, register

_PAUSE_WORDS = {"暂停", "先不用", "稍后", "退出", "停止", "stop", "pause", "later"}


@register(
    "employee_insight_plugin",
    "dc_agent",
    "员工需求洞察闭环：飞书私聊陪跑、卡点记录、候选洞察入口",
    "0.1.0",
)
class EmployeeInsightPlugin(Star):
    def __init__(self, context: Context) -> None:
        super().__init__(context)
        self.store: EmployeeInsightStore | None = None
        self.enabled = True

    async def initialize(self) -> None:
        cfg = self.context.get_config() if hasattr(self.context, "get_config") else {}
        if isinstance(cfg, dict):
            self.enabled = bool(cfg.get("enabled", True))
        data_dir = Path(
            getattr(
                self.context,
                "employee_insight_data_dir",
                Path(__file__).resolve().parents[3] / "data",
            )
        )
        data_dir.mkdir(parents=True, exist_ok=True)
        self.store = EmployeeInsightStore(data_dir / "employee_insight.db")
        await self.store.initialize()
        self.context.employee_insight_store = self.store
        logger.info(
            "[employee_insight] store 启动：%s", data_dir / "employee_insight.db"
        )

    @filter.event_message_type(EventMessageType.PRIVATE_MESSAGE, priority=75)
    async def on_private_message(self, event: AstrMessageEvent):
        if not self.enabled or self.store is None:
            return
        if not self._is_private_lark_event(event):
            return

        text = (getattr(event, "message_str", "") or "").strip()
        if not text:
            return

        if self._is_pause_text(text):
            session = await self._create_session(
                event,
                status=EmployeeInsightSessionStatus.MUTED,
                scenario_id="opt_out",
                original_request=text,
                summary="员工选择暂停或退出需求洞察触达。",
            )
            await self.store.append_event(
                InsightEvent(
                    event_id=uuid.uuid4().hex,
                    session_id=session.session_id,
                    event_type="opt_out",
                    actor="employee",
                    payload={"text": text},
                )
            )
            event.set_extra("employee_insight_session_id", session.session_id)
            self._reply(event, "收到，我先暂停主动找你。你之后随时可以私聊我继续使用。")
            return

        scenario_id = self._infer_scenario(text)
        await self.store.mark_profile_engaged(str(event.get_sender_id() or ""))
        session = await self._create_session(
            event,
            status=EmployeeInsightSessionStatus.ENGAGED,
            scenario_id=scenario_id,
            original_request=text,
            summary="员工通过飞书私聊进入需求洞察任务陪跑。",
        )
        event.set_extra("employee_insight_session_id", session.session_id)
        await self.store.append_event(
            InsightEvent(
                event_id=uuid.uuid4().hex,
                session_id=session.session_id,
                event_type="employee_replied",
                actor="employee",
                payload={"text": text, "scenario_id": scenario_id},
            )
        )
        await self.store.append_event(
            InsightEvent(
                event_id=uuid.uuid4().hex,
                session_id=session.session_id,
                event_type="task_started",
                actor="system",
                payload={
                    "scenario_id": scenario_id,
                    "coaching_mode": "lark_dm",
                },
            )
        )

    async def _create_session(
        self,
        event: AstrMessageEvent,
        *,
        status: EmployeeInsightSessionStatus,
        scenario_id: str,
        original_request: str,
        summary: str,
    ) -> EmployeeInsightSession:
        assert self.store is not None
        sender_id = str(event.get_sender_id() or "")
        session = EmployeeInsightSession(
            session_id=uuid.uuid4().hex,
            employee_id=sender_id,
            channel="lark_dm",
            trigger_type="employee_reply",
            status=status,
            department_id=str(event.get_extra("department_id") or "")
            if hasattr(event, "get_extra")
            else "",
            scenario_id=scenario_id,
            original_request=original_request[:4000],
            normalized_goal=original_request[:500],
            metadata={
                "platform_id": event.get_platform_id() or "",
                "platform_name": event.get_platform_name() or "",
                "session_id": getattr(event, "unified_msg_origin", "") or "",
                "sender_name": event.get_sender_name() or "",
            },
            summary=summary,
        )
        await self.store.upsert_session(session)
        await self.store.record_audit(
            action="session_created",
            actor="employee_insight_plugin",
            target_id=session.session_id,
            detail={
                "status": status.value,
                "scenario_id": scenario_id,
                "channel": "lark_dm",
            },
        )
        return session

    def _is_private_lark_event(self, event: AstrMessageEvent) -> bool:
        if (event.get_platform_name() or "").lower() != "lark":
            return False
        msg_obj = getattr(event, "message_obj", None)
        msg_type = str(getattr(msg_obj, "type", "") or "")
        if msg_type == "GroupMessage":
            return False
        try:
            if event.get_group_id():
                return False
        except Exception:  # noqa: BLE001
            pass
        return True

    def _is_pause_text(self, text: str) -> bool:
        compact = "".join(text.lower().split())
        return compact in _PAUSE_WORDS

    def _infer_scenario(self, text: str) -> str:
        if any(word in text for word in ("通知", "文案", "公告")):
            return "write_notice"
        if any(word in text for word in ("汇报", "报告", "总结")):
            return "generate_report"
        if any(word in text for word in ("制度", "流程", "怎么查")):
            return "lookup_policy"
        if any(word in text for word in ("不知道", "不会用", "怎么用")):
            return "unknown_how_to_start"
        return "general_need"

    def _reply(self, event: AstrMessageEvent, text: str) -> None:
        event.set_result(MessageEventResult().message(text).use_t2i(False).stop_event())
