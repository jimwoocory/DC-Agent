"""员工需求洞察闭环插件。

员工侧只处理飞书私聊：记录真实任务、卡点、暂停/退出意图，并把结构化
session/event 写入 employee insight 引擎。主动触达调度与真实发送由后续
受控任务调用，不在消息处理器里做群发副作用。
"""

from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dc_engines.employee_insight_loop import (
    EmployeeInsightProfile,
    EmployeeInsightSession,
    EmployeeInsightSessionStatus,
    EmployeeInsightStore,
    InsightEvent,
    PilotStatus,
    TaskStatus,
)

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.star import Context, Star, register

_PAUSE_WORDS = {"暂停", "先不用", "稍后", "退出", "停止", "stop", "pause", "later"}
_VERIFICATION_JOIN_WORDS = {
    "加入灰度验证",
    "加入灰度",
    "灰度验证",
    "登记测试",
    "注册测试",
}
_DEFAULT_LARK_PLATFORM_MARKERS = (
    "lark",
    "feishu",
    "飞书",
    "巅池-agent小助手",
    "agent小助手",
)
_NON_TASK_COMMAND_PREFIXES = ("/new", "/stop", "/reset", "/help")
_GREETING_RE = re.compile(
    r"^(?:你好|您好|嗨|hi|hello|在吗)(?:[，, ]*(?:小助手|助手))?[呀啊哦!！。,. ]*$",
    re.IGNORECASE,
)
_NEGATIVE_FEEDBACK_RE = re.compile(
    r"(?:不行|不对|不好用|没解决|没有解决|太慢|卡住|失败|报错|答非所问|还是不行)",
    re.IGNORECASE,
)


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
        self.config: dict = {}

    async def initialize(self) -> None:
        cfg = self.context.get_config() if hasattr(self.context, "get_config") else {}
        self.config = cfg if isinstance(cfg, dict) else {}
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
        self.context.employee_insight_link_task = self.link_task_lifecycle
        self.context.employee_insight_update_task = self.update_task_lifecycle
        logger.info(
            "[employee_insight] store 启动：%s", data_dir / "employee_insight.db"
        )
        await self._register_daily_outreach_job()

    @filter.event_message_type(EventMessageType.PRIVATE_MESSAGE, priority=110)
    async def on_private_message(self, event: AstrMessageEvent):
        if not self.enabled or self.store is None:
            return

        text = (getattr(event, "message_str", "") or "").strip()
        if not text:
            return
        if not self._is_private_lark_event(event):
            if self._is_verification_join_text(text):
                logger.warning(
                    "[employee_insight] 收到灰度登记口令但未识别为飞书私聊：platform_name=%s platform_id=%s origin=%s group_id=%s msg_type=%s",
                    event.get_platform_name() or "",
                    event.get_platform_id() or "",
                    getattr(event, "unified_msg_origin", "") or "",
                    self._safe_group_id(event),
                    self._message_type(event),
                )
            return

        timeout = float(self.config.get("side_effect_timeout_seconds", 1.5) or 0)
        try:
            if timeout > 0:
                await asyncio.wait_for(
                    self._handle_private_message(event, text),
                    timeout=timeout,
                )
            else:
                await self._handle_private_message(event, text)
        except TimeoutError:
            logger.warning(
                "[employee_insight] 私聊旁路处理超时，已放行主聊天：origin=%s",
                getattr(event, "unified_msg_origin", "") or "",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[employee_insight] 私聊旁路处理失败，已放行主聊天：%s",
                exc,
            )

    async def _handle_private_message(self, event: AstrMessageEvent, text: str) -> None:
        assert self.store is not None
        if self._is_verification_join_text(text):
            await self._register_verification_profile(event)
            self._reply(
                event,
                "已把你加入灰度验证测试名单。现在后台可以直接用你这个测试账号跑一键灰度验证，不需要手填 open_id。",
            )
            return

        if self._is_non_task_text(text):
            return

        if not await self._message_tracking_allowed(event):
            return

        await self._ensure_observed_profile(event)

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

        if _NEGATIVE_FEEDBACK_RE.search(text):
            active = await self.store.find_latest_active_session(
                str(event.get_sender_id() or "")
            )
            if active is not None:
                friction = text[:300]
                friction_points = list(active.friction_points)
                if friction not in friction_points:
                    friction_points.append(friction)
                updated = replace(
                    active,
                    status=EmployeeInsightSessionStatus.BLOCKED,
                    task_status=TaskStatus.BLOCKED,
                    satisfaction="negative",
                    friction_points=friction_points[-5:],
                )
                await self.store.upsert_session(updated)
                await self.store.append_event(
                    InsightEvent(
                        event_id=uuid.uuid4().hex,
                        session_id=updated.session_id,
                        event_type="friction_reported",
                        actor="employee",
                        payload={"text": friction},
                    )
                )
                event.set_extra("employee_insight_session_id", updated.session_id)
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

    async def link_task_lifecycle(
        self,
        event: AstrMessageEvent,
        task_id: str,
        *,
        source: str = "",
    ) -> None:
        """Link a Harness task to the employee insight session.

        Args:
            event: Original employee message event.
            task_id: Harness task identifier.
            source: Component that created or linked the task.
        """
        if self.store is None or not task_id:
            return
        session_id = str(event.get_extra("employee_insight_session_id") or "")
        if not session_id:
            return
        session = await self.store.get_session(session_id)
        if session is None:
            return
        updated = replace(
            session,
            task_status=TaskStatus.RUNNING,
            metadata={
                **session.metadata,
                "harness_task_id": task_id,
                "task_link_source": source,
            },
        )
        await self.store.upsert_session(updated)
        await self.store.append_event(
            InsightEvent(
                event_id=uuid.uuid4().hex,
                session_id=session_id,
                event_type="task_linked",
                actor="system",
                payload={"task_id": task_id, "source": source},
            )
        )

    async def update_task_lifecycle(
        self,
        task_id: str,
        *,
        status: str,
        source: str = "",
    ) -> None:
        """Mirror an authoritative Harness state into employee insight.

        Args:
            task_id: Harness task identifier.
            status: Inbox/Harness lifecycle status.
            source: Component that emitted the lifecycle update.
        """
        if self.store is None or not task_id:
            return
        session = await self.store.find_session_by_task_id(task_id)
        if session is None:
            return
        mapping = {
            "in_progress": (
                TaskStatus.RUNNING,
                EmployeeInsightSessionStatus.ENGAGED,
                "task_running",
            ),
            "delivered": (
                TaskStatus.RESULT_DELIVERED,
                EmployeeInsightSessionStatus.ENGAGED,
                "task_result_delivered",
            ),
            "closed": (
                TaskStatus.COMPLETED,
                EmployeeInsightSessionStatus.COMPLETED,
                "task_completed",
            ),
            "confirmed": (
                TaskStatus.COMPLETED,
                EmployeeInsightSessionStatus.COMPLETED,
                "task_completed",
            ),
            "blocked": (
                TaskStatus.BLOCKED,
                EmployeeInsightSessionStatus.BLOCKED,
                "task_blocked",
            ),
            "ignored": (
                TaskStatus.ABANDONED,
                EmployeeInsightSessionStatus.SKIPPED,
                "task_abandoned",
            ),
        }
        target = mapping.get(status)
        if target is None:
            return
        task_status, session_status, event_type = target
        now = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
        updated = replace(
            session,
            status=session_status,
            task_status=task_status,
            updated_at=now,
        )
        await self.store.upsert_session(updated)
        await self.store.append_event(
            InsightEvent(
                event_id=uuid.uuid4().hex,
                session_id=session.session_id,
                event_type=event_type,
                actor="system",
                payload={
                    "task_id": task_id,
                    "status": status,
                    "source": source,
                },
                created_at=now,
            )
        )

    async def _message_tracking_allowed(self, event: AstrMessageEvent) -> bool:
        assert self.store is not None
        if not bool(self.config.get("pilot_only", True)):
            return True

        employee_id = str(event.get_sender_id() or "").strip()
        if not employee_id:
            return False
        profile = await self.store.get_profile(employee_id)
        if profile is None and bool(self.config.get("auto_observe_private_dm", False)):
            await self._ensure_observed_profile(event)
            profile = await self.store.get_profile(employee_id)
        if profile is None:
            event.set_extra(
                "employee_insight_candidate",
                {"mode": "observe_only", "reason": "pilot_only"},
            )
            logger.info(
                "[employee_insight] pilot_only 跳过非灰度私聊：employee_id=%s",
                employee_id[:12],
            )
            return False
        if profile.pilot_status != PilotStatus.ACTIVE:
            event.set_extra(
                "employee_insight_candidate",
                {
                    "mode": "observe_only",
                    "reason": f"pilot_status:{profile.pilot_status.value}",
                },
            )
            return False
        return True

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
        directory_employee = await self._get_directory_employee(sender_id)
        event_department_id = (
            str(event.get_extra("department_id") or "")
            if hasattr(event, "get_extra")
            else ""
        )
        department_id = (
            str(getattr(directory_employee, "department", "") or "")
            or event_department_id
        )
        role = str(getattr(directory_employee, "role", "") or "")
        session = EmployeeInsightSession(
            session_id=uuid.uuid4().hex,
            employee_id=sender_id,
            channel="lark_dm",
            trigger_type="employee_reply",
            status=status,
            department_id=department_id,
            role=role,
            scenario_id=scenario_id,
            original_request=original_request[:4000],
            normalized_goal=original_request[:500],
            metadata={
                "platform_id": event.get_platform_id() or "",
                "platform_name": event.get_platform_name() or "",
                "session_id": getattr(event, "unified_msg_origin", "") or "",
                "sender_name": event.get_sender_name() or "",
                "directory_display_name": str(
                    getattr(directory_employee, "display_name", "") or ""
                ),
                "directory_source": "employees_db" if directory_employee else "",
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
        if not self._is_lark_platform_event(event):
            return False
        if self._event_is_private_chat(event):
            return True
        msg_type = self._message_type(event)
        if "group" in msg_type.lower():
            return False
        if self._safe_group_id(event):
            return False
        return True

    def _event_is_private_chat(self, event: AstrMessageEvent) -> bool:
        try:
            return bool(event.is_private_chat())
        except Exception:  # noqa: BLE001
            return False

    def _is_lark_platform_event(self, event: AstrMessageEvent) -> bool:
        evidence = " ".join(
            value
            for value in (
                str(event.get_platform_name() or ""),
                str(event.get_platform_id() or ""),
                str(getattr(event, "unified_msg_origin", "") or ""),
            )
            if value
        ).lower()
        markers = self.config.get("platform_allow_keywords")
        if not isinstance(markers, list) or not markers:
            markers = list(_DEFAULT_LARK_PLATFORM_MARKERS)
        return any(str(marker).lower() in evidence for marker in markers)

    def _message_type(self, event: AstrMessageEvent) -> str:
        msg_obj = getattr(event, "message_obj", None)
        return str(getattr(msg_obj, "type", "") or "")

    def _safe_group_id(self, event: AstrMessageEvent) -> str:
        try:
            return str(event.get_group_id() or "")
        except Exception:  # noqa: BLE001
            return ""

    def _is_pause_text(self, text: str) -> bool:
        compact = "".join(text.lower().split())
        return compact in _PAUSE_WORDS

    def _is_non_task_text(self, text: str) -> bool:
        normalized = text.strip()
        return (
            normalized.startswith("__card_action__:")
            or normalized.startswith(_NON_TASK_COMMAND_PREFIXES)
            or _GREETING_RE.fullmatch(normalized) is not None
        )

    def _is_verification_join_text(self, text: str) -> bool:
        compact = "".join(text.lower().split())
        return compact in _VERIFICATION_JOIN_WORDS

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

    async def _ensure_observed_profile(self, event: AstrMessageEvent) -> None:
        assert self.store is not None
        employee_id = str(event.get_sender_id() or "").strip()
        if not employee_id:
            return
        existing = await self.store.get_profile(employee_id)
        directory_employee = await self._get_directory_employee(employee_id)
        auto_verification_scope = bool(
            self.config.get("auto_verification_scope_from_dm", True)
        )
        existing_metadata = existing.metadata if existing else {}
        profile = EmployeeInsightProfile(
            employee_id=employee_id,
            employee_hash=(existing.employee_hash if existing else employee_id),
            display_name=(
                str(getattr(directory_employee, "display_name", "") or "")
                or str(event.get_sender_name() or "")
            ),
            department_id=(
                str(getattr(directory_employee, "department", "") or "")
                or (existing.department_id if existing else "")
            ),
            role=(
                str(getattr(directory_employee, "role", "") or "")
                or (existing.role if existing else "自然私聊观察")
            ),
            pilot_status=(existing.pilot_status if existing else PilotStatus.ACTIVE),
            preferred_touch_time=existing.preferred_touch_time if existing else "",
            last_outreach_at=existing.last_outreach_at if existing else "",
            unanswered_outreach_count=(
                existing.unanswered_outreach_count if existing else 0
            ),
            metadata={
                **existing_metadata,
                "observed_from_dm": True,
                "verification_scope": bool(
                    existing_metadata.get("verification_scope", False)
                    or auto_verification_scope
                ),
                "platform_id": event.get_platform_id() or "",
                "platform_name": event.get_platform_name() or "",
                "directory_source": "employees_db" if directory_employee else "",
            },
        )
        await self.store.upsert_profile(profile)
        if existing is None:
            await self.store.record_audit(
                action="profile_observed_from_lark_dm",
                actor="employee_insight_plugin",
                target_id=employee_id,
                detail={
                    "verification_scope": profile.metadata["verification_scope"],
                    "platform_id": event.get_platform_id() or "",
                },
            )
            logger.info(
                "[employee_insight] 自然私聊已登记观察对象：employee_id=%s platform_name=%s platform_id=%s",
                employee_id,
                event.get_platform_name() or "",
                event.get_platform_id() or "",
            )

    async def _register_verification_profile(self, event: AstrMessageEvent) -> None:
        assert self.store is not None
        employee_id = str(event.get_sender_id() or "").strip()
        if not employee_id:
            return
        existing = await self.store.get_profile(employee_id)
        directory_employee = await self._get_directory_employee(employee_id)
        profile = EmployeeInsightProfile(
            employee_id=employee_id,
            employee_hash=(existing.employee_hash if existing else employee_id),
            display_name=(
                str(getattr(directory_employee, "display_name", "") or "")
                or str(event.get_sender_name() or "")
            ),
            department_id=(
                str(getattr(directory_employee, "department", "") or "")
                or (existing.department_id if existing else "")
            ),
            role=(
                str(getattr(directory_employee, "role", "") or "")
                or (existing.role if existing else "灰度测试")
            ),
            pilot_status=PilotStatus.ACTIVE,
            preferred_touch_time=existing.preferred_touch_time if existing else "",
            last_outreach_at=existing.last_outreach_at if existing else "",
            unanswered_outreach_count=(
                existing.unanswered_outreach_count if existing else 0
            ),
            metadata={
                **(existing.metadata if existing else {}),
                "verification_scope": True,
                "self_registered": True,
                "platform_id": event.get_platform_id() or "",
                "platform_name": event.get_platform_name() or "",
                "directory_source": "employees_db" if directory_employee else "",
            },
        )
        await self.store.upsert_profile(profile)
        await self.store.record_audit(
            action="verification_profile_registered",
            actor="employee_insight_plugin",
            target_id=employee_id,
            detail={
                "verification_scope": True,
                "platform_id": event.get_platform_id() or "",
            },
        )
        logger.info(
            "[employee_insight] 灰度验证账号已登记：employee_id=%s platform_name=%s platform_id=%s",
            employee_id,
            event.get_platform_name() or "",
            event.get_platform_id() or "",
        )

    async def _register_daily_outreach_job(self) -> None:
        if not self.config.get("daily_outreach_enabled", False):
            return
        cron_manager = getattr(self.context, "cron_job_manager", None)
        if cron_manager is None or not hasattr(cron_manager, "add_basic_job"):
            logger.warning("[employee_insight] cron manager 不可用，跳过每日触达任务")
            return
        await cron_manager.add_basic_job(
            name="员工需求洞察每日触达",
            cron_expression=str(self.config.get("daily_outreach_cron") or "0 10 * * *"),
            timezone=str(self.config.get("daily_outreach_timezone") or "Asia/Shanghai"),
            handler=self._run_daily_outreach_job,
            description="按试点画像执行员工需求洞察触达；默认 dry-run。",
            payload={},
            enabled=True,
            persistent=True,
        )

    async def _run_daily_outreach_job(self) -> dict:
        assert self.store is not None
        from dc_engines.employee_insight_loop import EmployeeInsightOutreachDispatcher
        from dc_engines.feishu_writer import FeishuPrivateMessageSender

        dry_run = not bool(self.config.get("daily_outreach_real_send_enabled", False))
        approved = bool(self.config.get("daily_outreach_approved", False)) and bool(
            self.config.get("daily_outreach_approval_token")
        )
        dispatcher = EmployeeInsightOutreachDispatcher(
            self.store,
            sender=FeishuPrivateMessageSender(),
        )
        try:
            return await dispatcher.dispatch_daily_outreach(
                limit=int(self.config.get("daily_outreach_limit") or 20),
                approved=approved,
                dry_run=dry_run,
                actor="employee_insight_cron",
                message_text=str(self.config.get("daily_outreach_message") or "")
                or None,
            )
        except PermissionError as exc:
            await self.store.record_audit(
                action="daily_outreach_blocked",
                actor="employee_insight_cron",
                target_id="daily_outreach",
                detail={"error": str(exc), "dry_run": dry_run},
            )
            return {"mode": "blocked", "error": str(exc)}

    async def _get_directory_employee(self, employee_id: str) -> Any | None:
        store = getattr(self.context, "employee_store", None)
        if store is None or not hasattr(store, "get_employee"):
            return None
        try:
            return await store.get_employee(employee_id)
        except Exception:  # noqa: BLE001
            logger.debug("[employee_insight] employee directory lookup failed")
            return None
