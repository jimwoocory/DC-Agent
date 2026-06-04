"""员工 onboarding state machine + 卡片驱动入职流程。

监听两类事件：
1. 飞书 lark **私聊普通消息**：
   - 新员工首次接入 + 还没 profile_filled → 推部门卡，启动流程
   - 已在 onboarding 但 stage='name' → 当成姓名输入存进 db → 推教程清单
2. 飞书 **卡片按钮回调**（lark_adapter 把它包成 `__card_action__:` 前缀的伪消息）：
   - select_dept → 存部门 → 推角色卡
   - select_role → 存角色 → 推姓名 prompt
   - open_lesson → 推单节教程
   - show_tutorial_list → 重推清单
   - start_quiz → 推第 1 题
   - submit_quiz → 判分 → 推反馈
   - next_quiz → 推下一题（最后一题后推结果卡）

未完成 onboarding 的员工，普通文字消息不会触发 LLM（用 event.stop_event 拦住，
强制走完流程；防止员工绕过 onboarding 直接当豆包用）。
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any

from dc_engines.card_runtime import send_card_via_runtime
from dc_engines.employee_directory import Employee, sync_from_feishu
from dc_engines.feishu_card_streamer import (
    QUIZ_QUESTIONS,
    TUTORIALS,
    FeishuCardStreamer,
    build_onboarding_dept_card,
    build_onboarding_name_prompt_card,
    build_onboarding_role_card,
    build_onboarding_tutorial_list_card,
    build_quiz_feedback_card,
    build_quiz_question_card,
    build_quiz_result_card,
    build_tutorial_lesson_card,
    ensure_streamers_on_context,
    get_quiz_for_dept,
    next_lesson_id,
)
from dc_engines.feishu_reader import FeishuClient
from dc_engines.feishu_writer import ChatCreator

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.star import Context, Star, register

# 只在 lark 私聊触发 onboarding（群里员工不该被引导）
LARK_PLATFORMS = {"巅池-Agent小助手"}  # 内测主要走小助手
COMPLETED_STAGES = {"done", "passed", "invited", "joined"}
ONBOARDING_PREF_KEY = "_onboarding"

# onboarding 自家发出的卡片按钮 action 白名单。
# 其它 plugin（如 feishu_pet_assistant）的卡片回调不应被这里 stop_event。
ONBOARDING_CARD_ACTIONS = {
    "start_onboarding_from_invite",
    "select_dept",
    "select_role",
    "open_lesson",
    "continue_tutorial",
    "show_tutorial_list",
    "start_quiz",
    "submit_quiz",
    "next_quiz",
    "review_and_retry",
    "retry_quiz",
    "show_result",
    "noop",
}
# 入职引导链：lesson 卡的倒计时默认值（秒）
LESSON_AUTO_ADVANCE_SEC = 60
FIRST_LESSON_ID = "lesson_reasoning"

# 部门 / 角色 中文映射（卡片显示用）
# 2026-05-22 按职能部组织架构调整为 7 部门，ID 跟 department_workflows.defaults 对齐。
DEPT_DISPLAY = {
    "executive_office": "总经办",
    "client_dept": "客户部",
    "planning": "策划",
    "brand_publicity": "品宣部",
    "execution_ops": "执行运营",
    "general_affairs": "综合部",
    "finance": "财务部",
    # 历史数据兼容：旧员工的 dept_code 仍能 lookup 到显示名，避免 KeyError
    "marketing": "客户部",
    "strategy": "策划",
    "branding": "品宣部",
    "exec_office": "总经办",
    "operations": "执行运营",
    "film": "品宣部",  # 影视部已下线，归并到品宣部
    "client": "客户部",
    "planning_dept": "策划",
    "general": "综合部",
    "comprehensive": "综合部",
}
ROLE_DISPLAY = {
    "director": "总监",
    "manager": "经理",
    "specialist": "专员",
    "intern": "实习生",
}

# 内测群邀请链接（用户测试通过后给）
DEFAULT_INVITE_LINK = "https://o0ain5w98jh.feishu.cn/q/...（待补真实链接）"

# 通过门槛策略：全对才通过（题数 = 5 通用 + 1 部门差异 = 6）
# 实际门槛在运行时从 state.quiz_total 取，这里只是兜底值（dept_code 缺失时回退到 5 题通用题）
PASS_THRESHOLD_FALLBACK = len(QUIZ_QUESTIONS)


@register(
    "employee_onboarding",
    "dc_agent",
    "员工入职引导 · 飞书卡片驱动身份采集 + 教程 + 准入测试",
    "1.0.0",
)
class EmployeeOnboardingPlugin(Star):
    def __init__(self, context: Context, config=None) -> None:
        super().__init__(context, config)
        cfg = config or {}
        force_disable = os.environ.get("EMPLOYEE_ONBOARDING_FORCE_DISABLE") == "1"
        self.enabled = bool(cfg.get("enabled", False)) and not force_disable
        self.maintenance_mode = bool(cfg.get("maintenance_mode", True)) or force_disable
        self.active_outreach_enabled = self._is_operational() and bool(
            cfg.get("active_outreach_enabled", False)
        )
        self.sync_contacts_before_scan = bool(
            cfg.get("sync_contacts_before_scan", False)
        )
        self.scan_interval_seconds = max(
            60, int(cfg.get("scan_interval_seconds", 3600))
        )
        self.initial_scan_delay_seconds = max(
            0, int(cfg.get("initial_scan_delay_seconds", 120))
        )
        self.reminder_cooldown_hours = max(
            1, int(cfg.get("reminder_cooldown_hours", 24))
        )
        self.max_reminders_per_employee = max(
            1, int(cfg.get("max_reminders_per_employee", 3))
        )
        self.scan_limit = max(1, int(cfg.get("scan_limit", 500)))
        self.max_outreach_per_scan = max(1, int(cfg.get("max_outreach_per_scan", 20)))
        self.outreach_send_delay_seconds = max(
            0.0, float(cfg.get("outreach_send_delay_seconds", 1.0))
        )
        self.target_platform_id = str(
            cfg.get("target_platform_id", "巅池-Agent小助手")
        ).strip()
        self.invite_link = str(cfg.get("invite_link", DEFAULT_INVITE_LINK)).strip()
        self.auto_invite_to_chat = bool(cfg.get("auto_invite_to_chat", False))
        self.internal_test_chat_id = str(cfg.get("internal_test_chat_id", "")).strip()
        self._outreach_task: asyncio.Task | None = None
        self._scan_lock = asyncio.Lock()
        self._chat_creator: ChatCreator | None = None
        # 入职引导链：lesson 卡倒计时 task 按 open_id 单例。
        # 点任何按钮或员工主动启动测试 → cancel；推下一节卡 → 重启。
        self._lesson_timeout_tasks: dict[str, asyncio.Task] = {}

    async def initialize(self) -> None:
        if not self._is_operational():
            logger.info("[onboarding] 插件处于关闭或维护模式，不启动自动流程")
            return
        if self.auto_invite_to_chat and self.internal_test_chat_id:
            try:
                self._chat_creator = ChatCreator()
            except Exception as exc:  # noqa: BLE001
                logger.warning("[onboarding] ChatCreator 初始化失败：%s", exc)
        if self.active_outreach_enabled:
            self._outreach_task = asyncio.create_task(
                self._outreach_loop(),
                name="employee-onboarding-outreach",
            )
            logger.info(
                "[onboarding] 主动触达巡检已启动 interval=%ss cooldown=%sh",
                self.scan_interval_seconds,
                self.reminder_cooldown_hours,
            )

    async def terminate(self) -> None:
        if self._outreach_task:
            self._outreach_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._outreach_task
            self._outreach_task = None
        # 清理所有还在等的 lesson 倒计时 task
        for task in list(self._lesson_timeout_tasks.values()):
            if not task.done():
                task.cancel()
        self._lesson_timeout_tasks.clear()

    # ─────────────────── 辅助工具 ───────────────────

    def _store(self):
        """拿 concierge_plugin 启动的 EmployeeStore（共享）。"""
        return getattr(self.context, "employee_store", None)

    def _is_operational(self) -> bool:
        return self.enabled and not self.maintenance_mode

    def _is_admin_event(self, event: AstrMessageEvent) -> bool:
        try:
            return bool(event.is_admin())
        except Exception:  # noqa: BLE001
            return False

    def _is_trusted_card_action(self, event: AstrMessageEvent) -> bool:
        msg = getattr(event, "message_obj", None)
        return (
            getattr(event, "is_card_action", False) is True
            or getattr(msg, "is_card_action", False) is True
        )

    def _valid_invite_link(self) -> str:
        link = self.invite_link.strip()
        if not link:
            return ""
        if "…" in link or "..." in link or "（待补" in link or "(待补" in link:
            return ""
        return link

    def _is_lark(self, event: AstrMessageEvent) -> bool:
        platform_id = event.get_platform_id() or ""
        return platform_id in LARK_PLATFORMS or platform_id == self.target_platform_id

    def _open_id(self, event: AstrMessageEvent) -> str:
        return event.get_sender_id() or ""

    def _chat_id(self, event: AstrMessageEvent) -> tuple[str, str]:
        """返回 (chat_id, receive_id_type) 用于发新卡片。"""
        raw = getattr(event.message_obj, "raw_message", None)
        chat_id = getattr(raw, "chat_id", None) or ""
        if not chat_id:
            chat_id = event.get_group_id() or event.get_sender_id() or ""
        receive_id_type = "chat_id" if chat_id.startswith("oc_") else "open_id"
        return chat_id, receive_id_type

    def _streamer(self, platform_id: str) -> FeishuCardStreamer | None:
        streamers = ensure_streamers_on_context(self.context)
        return streamers.get(platform_id)

    def _target_streamer(self) -> FeishuCardStreamer | None:
        if not self.target_platform_id:
            return None
        return self._streamer(self.target_platform_id)

    @staticmethod
    def _card_type_for_stage(stage: str) -> str:
        if stage == "dept":
            return "onboarding_department"
        if stage in {"role", "name"}:
            return "onboarding_role"
        if stage in {"quiz", "quiz_feedback", "quiz_result"}:
            return "training_quiz"
        return "training_lesson"

    async def _send_card(
        self,
        event: AstrMessageEvent,
        card: dict,
        *,
        card_type: str = "training_lesson",
        detail: str = "employee onboarding card",
    ) -> str | None:
        """发新卡，返回 message_id。"""
        chat_id, rtype = self._chat_id(event)
        streamer = self._streamer(event.get_platform_id() or "")
        if not streamer or not chat_id:
            return None
        stream = await send_card_via_runtime(
            streamer,
            card_type=card_type,
            chat_id=chat_id,
            receive_id_type=rtype,
            card=card,
            platform_id=event.get_platform_id() or self.target_platform_id,
            event="start",
            detail=detail,
        )
        return stream.message_id if stream else None

    async def _get_onboarding_state(self, open_id: str) -> dict:
        """读员工的 onboarding 状态（preferences['_onboarding']）。"""
        store = self._store()
        if not store:
            return {}
        emp = await store.get_employee(open_id)
        if not emp:
            return {}
        prefs = emp.preferences or {}
        return prefs.get(ONBOARDING_PREF_KEY, {})

    async def _set_onboarding_state(self, open_id: str, **updates) -> None:
        """合并更新 onboarding 状态。"""
        store = self._store()
        if not store:
            return
        emp = await store.get_employee(open_id)
        if not emp:
            return
        prefs = dict(emp.preferences or {})
        state = dict(prefs.get(ONBOARDING_PREF_KEY, {}))
        state.update(updates)
        prefs[ONBOARDING_PREF_KEY] = state
        await store.update_profile(open_id, preferences=prefs)

    async def _is_onboarded(self, open_id: str) -> bool:
        state = await self._get_onboarding_state(open_id)
        return state.get("stage") in COMPLETED_STAGES

    def _state_for(self, emp: Employee) -> dict[str, Any]:
        prefs = emp.preferences or {}
        state = prefs.get(ONBOARDING_PREF_KEY, {})
        return state if isinstance(state, dict) else {}

    def _build_entry_card_for_employee(self, emp: Employee) -> tuple[dict, str]:
        state = self._state_for(emp)
        stage = str(state.get("stage") or "").strip()
        if stage == "dept" or not emp.department:
            return build_onboarding_dept_card(welcome_name=emp.display_name), "dept"
        if stage == "role" or not emp.role:
            return build_onboarding_role_card(dept_name=emp.department), "role"
        if stage == "name" or not emp.display_name:
            return build_onboarding_name_prompt_card(role_name=emp.role), "name"
        dept_code = state.get("dept_code") if isinstance(state, dict) else None
        return build_onboarding_tutorial_list_card(
            display_name=emp.display_name, dept_code=dept_code
        ), ("quiz_failed" if stage == "quiz_failed" else "tutorial")

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _parse_iso(value: Any) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    async def _send_entry_card_to_event(
        self,
        event: AstrMessageEvent,
        emp: Employee,
        *,
        reason: str,
    ) -> str | None:
        card, stage = self._build_entry_card_for_employee(emp)
        message_id = await self._send_card(
            event,
            card,
            card_type=self._card_type_for_stage(stage),
            detail=f"employee onboarding entry: {reason}",
        )
        if message_id:
            await self._set_onboarding_state(
                emp.open_id,
                stage=stage,
                last_outreach_at=self._now_iso(),
                last_outreach_message_id=message_id,
                last_outreach_reason=reason,
                platform_id=event.get_platform_id() or self.target_platform_id,
            )
        return message_id

    async def _send_entry_card_to_employee(
        self,
        emp: Employee,
        *,
        reason: str,
        force: bool = False,
    ) -> bool:
        streamer = self._target_streamer()
        if streamer is None:
            logger.debug("[onboarding] 无可用飞书 streamer，跳过主动触达")
            return False

        card, stage = self._build_entry_card_for_employee(emp)
        stream = await send_card_via_runtime(
            streamer,
            card_type=self._card_type_for_stage(stage),
            chat_id=emp.open_id,
            receive_id_type="open_id",
            card=card,
            platform_id=self.target_platform_id,
            event="start",
            detail=f"employee onboarding outreach: {reason}",
        )
        state = self._state_for(emp)
        outreach_count = int(state.get("outreach_count") or 0)
        if stream is None:
            await self._set_onboarding_state(
                emp.open_id,
                last_outreach_error_at=self._now_iso(),
                last_outreach_error="send_card_failed",
            )
            return False
        await self._set_onboarding_state(
            emp.open_id,
            stage=stage,
            outreach_count=outreach_count + 1,
            last_outreach_at=self._now_iso(),
            last_outreach_message_id=stream.message_id,
            last_outreach_reason=reason,
            platform_id=self.target_platform_id,
            force_outreach=force,
        )
        logger.info(
            "[onboarding] 主动触达 open_id=%s stage=%s count=%d",
            emp.open_id[:12],
            stage,
            outreach_count + 1,
        )
        return True

    def _needs_outreach(self, emp: Employee, *, force: bool = False) -> bool:
        if not emp.open_id.startswith("ou_"):
            return False
        state = self._state_for(emp)
        stage = str(state.get("stage") or "").strip()
        if stage in COMPLETED_STAGES:
            return False
        if state.get("paused") or stage == "do_not_contact":
            return False
        if force:
            return True
        outreach_count = int(state.get("outreach_count") or 0)
        if outreach_count >= self.max_reminders_per_employee:
            return False
        last_outreach_at = self._parse_iso(state.get("last_outreach_at"))
        if last_outreach_at is None:
            return True
        elapsed_hours = (
            datetime.now(timezone.utc) - last_outreach_at
        ).total_seconds() / 3600
        return elapsed_hours >= self.reminder_cooldown_hours

    async def _sync_contacts_if_enabled(self, store) -> None:
        if not self.sync_contacts_before_scan:
            return
        try:
            client = FeishuClient()
            report = await sync_from_feishu(
                store,
                client,
                platform_id=self.target_platform_id or "lark",
            )
            if report.success:
                logger.info(
                    "[onboarding] 通讯录同步完成 added=%d updated=%d skipped=%d",
                    report.users_added,
                    report.users_updated,
                    report.users_skipped,
                )
            else:
                logger.warning("[onboarding] 通讯录同步失败：%s", report.error)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[onboarding] 通讯录同步异常：%s", exc)

    async def _run_outreach_scan(self, *, force: bool = False) -> dict[str, int]:
        if not self._is_operational():
            return {
                "disabled": 1,
                "store_missing": 0,
                "scanned": 0,
                "eligible": 0,
                "sent": 0,
                "failed": 0,
                "skipped": 0,
            }
        async with self._scan_lock:
            store = self._store()
            if not store:
                return {"store_missing": 1, "scanned": 0, "sent": 0, "failed": 0}
            await self._sync_contacts_if_enabled(store)
            employees = await store.list_employees(limit=self.scan_limit)
            report = {
                "store_missing": 0,
                "scanned": len(employees),
                "eligible": 0,
                "sent": 0,
                "failed": 0,
                "skipped": 0,
            }
            for emp in employees:
                if report["sent"] >= self.max_outreach_per_scan:
                    break
                if not self._needs_outreach(emp, force=force):
                    report["skipped"] += 1
                    continue
                report["eligible"] += 1
                ok = await self._send_entry_card_to_employee(
                    emp,
                    reason="manual_scan" if force else "scheduled_scan",
                    force=force,
                )
                if ok:
                    report["sent"] += 1
                else:
                    report["failed"] += 1
                if self.outreach_send_delay_seconds:
                    await asyncio.sleep(self.outreach_send_delay_seconds)
            return report

    async def _outreach_loop(self) -> None:
        if self.initial_scan_delay_seconds:
            await asyncio.sleep(self.initial_scan_delay_seconds)
        while True:
            try:
                report = await self._run_outreach_scan()
                logger.info("[onboarding] 主动触达巡检完成：%s", report)
                await asyncio.sleep(self.scan_interval_seconds)
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                logger.warning("[onboarding] 主动触达巡检异常：%s", exc)
                await asyncio.sleep(self.scan_interval_seconds)

    def _reply(self, event: AstrMessageEvent, text: str) -> None:
        event.set_result(MessageEventResult().message(text).use_t2i(False).stop_event())

    # ─────────────────── 主消息钩子 ───────────────────

    @filter.event_message_type(EventMessageType.PRIVATE_MESSAGE)
    async def on_lark_private(self, event: AstrMessageEvent) -> None:
        if not self._is_lark(event):
            return
        if not self._is_operational():
            return
        store = self._store()
        if not store:
            return  # concierge 没就绪

        open_id = self._open_id(event)
        if not open_id:
            return

        text = (event.message_str or "").strip()

        if text == "__bot_p2p_chat_entered__":
            state = await self._get_onboarding_state(open_id)
            stage = str(state.get("stage") or "").strip()
            if stage not in COMPLETED_STAGES:
                await self._start_onboarding(event)
                event.stop_event()
            return

        # 1) 卡片按钮回调
        if text.startswith("__card_action__:"):
            # 先判断 action 是不是 onboarding 自家发出的卡按钮。
            # 不是（如 pet_view_tasks 等其它 plugin 的卡）→ 不 stop_event，
            # 让事件继续传播到下一个 plugin handler。
            action_name = self._peek_card_action_name(text)
            if action_name not in ONBOARDING_CARD_ACTIONS:
                logger.debug("[onboarding] 非自家卡片回调放行 action=%r", action_name)
                return
            await self._handle_card_action(event, text)
            event.stop_event()  # 自家卡才阻止其他插件继续处理
            return

        # 1.5) Explicit reset command for admins only.
        if text in ("/重新入职", "/onboard", "/reonboard", "重新入职"):
            if not self._is_admin_event(event):
                logger.warning(
                    "[onboarding] 非管理员尝试重置 onboarding open_id=%s",
                    open_id[:12],
                )
                self._reply(event, "权限不足：只有管理员可以重置入职流程。")
                event.stop_event()
                return
            await self._reset_onboarding(event)
            event.stop_event()
            return

        # 2) 普通文字消息 —— 看是否在 onboarding 流程中
        state = await self._get_onboarding_state(open_id)
        stage = state.get("stage")
        emp = await store.get_employee(open_id)

        # ─── 关键：先处理 onboarding 进行中状态（避免被 reset 覆盖）───

        # 在等姓名输入 → 当成姓名存（最高优先级，否则会被 needs_onboarding 误重启）
        if stage == "name":
            await self._handle_name_input(event, text)
            event.stop_event()
            return

        # 在等按钮交互（dept/role/tutorial/quiz_active）→ 提示用按钮
        if stage in ("dept", "role", "tutorial", "quiz_active", "quiz_failed"):
            await event.send(
                event.plain_result(
                    "📋 请用我刚才发的卡片按钮继续，完成 onboarding 后再聊业务～"
                )
            )
            event.stop_event()
            return

        # 已 onboarded → 放行（其他插件正常处理）
        if stage in COMPLETED_STAGES:
            return

        # ─── 没有任何 onboarding 状态 → 启动新流程 ───
        needs_onboarding = emp is None or not state or stage not in COMPLETED_STAGES
        if needs_onboarding:
            await self._start_onboarding(event)
            event.stop_event()
            return

    # ─────────────────── Onboarding 启动 ───────────────────

    async def _start_onboarding(self, event: AstrMessageEvent) -> None:
        """新员工首次接入 → 发部门卡 + 标 stage='dept'。"""
        open_id = self._open_id(event)
        store = self._store()
        if not store:
            return
        platform_id = event.get_platform_id() or ""
        emp, _ = await store.get_or_create(open_id, platform_id=platform_id)
        message_id = await self._send_entry_card_to_event(
            event,
            emp,
            reason="private_first_touch",
        )
        logger.info(
            "[onboarding] 新员工 open_id=%s 启动 onboarding message_id=%s",
            open_id[:12],
            message_id,
        )

    async def _reset_onboarding(self, event: AstrMessageEvent) -> None:
        """显式重置（清 display_name/dept/role/preferences），重新走流程。"""
        open_id = self._open_id(event)
        # 重置 → 清理可能还在跑的 lesson 倒计时
        self._cancel_lesson_timeout(open_id)
        store = self._store()
        if not store:
            return
        await store.update_profile(
            open_id,
            display_name="",
            department="",
            role="",
            preferences={},
        )
        await self._start_onboarding(event)
        logger.info("[onboarding] 重置员工 %s 的 onboarding", open_id[:12])

    # ─────────────────── 卡片回调处理 ───────────────────

    @staticmethod
    def _peek_card_action_name(text: str) -> str:
        """从 `__card_action__:{...}` 文本里轻量解析出 value.action 字符串。
        失败返回 ""。仅用于路由判断，不做真实 payload 处理。
        """
        try:
            payload = json.loads(text[len("__card_action__:") :])
        except Exception:  # noqa: BLE001
            return ""
        value = payload.get("value") if isinstance(payload, dict) else None
        if isinstance(value, dict):
            return str(value.get("action") or "")
        return ""

    async def _handle_card_action(self, event: AstrMessageEvent, text: str) -> None:
        if not self._is_operational():
            return
        if not self._is_trusted_card_action(event):
            logger.warning(
                "[onboarding] 拒绝非可信来源卡片动作 sender=%s",
                self._open_id(event)[:12],
            )
            return
        try:
            payload_str = text[len("__card_action__:") :]
            payload = json.loads(payload_str)
        except Exception as exc:
            logger.warning("[onboarding] 卡片 payload 解析失败：%s", exc)
            return

        value = payload.get("value", {}) or {}
        action = value.get("action", "")

        if action == "select_dept":
            await self._on_select_dept(event, value.get("dept", ""))
        elif action == "start_onboarding_from_invite":
            await self._on_start_onboarding_from_invite(event)
        elif action == "select_role":
            await self._on_select_role(event, value.get("role", ""))
        elif action == "open_lesson":
            await self._on_open_lesson(event, value.get("lesson_id", ""))
        elif action == "continue_tutorial":
            await self._on_continue_tutorial(event, value.get("current_lesson_id", ""))
        elif action == "show_tutorial_list":
            await self._on_show_tutorial_list(event)
        elif action == "start_quiz":
            await self._on_start_quiz(event)
        elif action == "submit_quiz":
            await self._on_submit_quiz(
                event, value.get("q_num", 1), value.get("choice", "")
            )
        elif action == "next_quiz":
            await self._on_next_quiz(event, value.get("q_num", 1))
        elif action == "review_and_retry":
            await self._on_review_and_retry(
                event,
                value.get("q_num", 1),
                value.get("lesson_id", ""),
            )
        elif action == "retry_quiz":
            await self._on_retry_quiz(event, value.get("q_num", 1))
        elif action == "show_result":
            await self._on_show_result(event)
        elif action == "noop":
            pass  # 占位按钮
        else:
            logger.debug("[onboarding] 未识别的 action: %r", action)

    # ─────────────────── 各 action 实现 ───────────────────

    async def _on_start_onboarding_from_invite(self, event: AstrMessageEvent) -> None:
        open_id = self._open_id(event)
        store = self._store()
        if not open_id or not store:
            return

        platform_id = event.get_platform_id() or self.target_platform_id
        emp, _ = await store.get_or_create(open_id, platform_id=platform_id)
        state = self._state_for(emp)
        stage = str(state.get("stage") or "").strip()
        if stage in COMPLETED_STAGES:
            await event.send(
                event.plain_result("你已经完成入职培训和答题，可以正常使用小助手功能。")
            )
            return

        message_id = await self._send_entry_card_to_event(
            event,
            emp,
            reason="invite_card_action",
        )
        logger.info(
            "[onboarding] 邀请卡触发 onboarding open_id=%s stage=%s message_id=%s",
            open_id[:12],
            self._state_for(emp).get("stage") or stage or "dept",
            message_id,
        )

    async def _on_select_dept(self, event: AstrMessageEvent, dept_code: str) -> None:
        if dept_code not in DEPT_DISPLAY:
            return
        open_id = self._open_id(event)
        # 重新选部门 → 取消旧倒计时（避免上次教程没看完，新部门又冒出来）
        self._cancel_lesson_timeout(open_id)
        store = self._store()
        if not store:
            return
        await store.update_profile(open_id, department=DEPT_DISPLAY[dept_code])
        await self._set_onboarding_state(open_id, stage="role", dept_code=dept_code)
        await self._send_card(
            event,
            build_onboarding_role_card(
                dept_name=DEPT_DISPLAY[dept_code],
            ),
            card_type="onboarding_role",
            detail="employee onboarding role selection",
        )

    async def _on_select_role(self, event: AstrMessageEvent, role_code: str) -> None:
        if role_code not in ROLE_DISPLAY:
            return
        open_id = self._open_id(event)
        self._cancel_lesson_timeout(open_id)
        store = self._store()
        if not store:
            return
        await store.update_profile(open_id, role=ROLE_DISPLAY[role_code])
        await self._set_onboarding_state(open_id, stage="name", role_code=role_code)
        await self._send_card(
            event,
            build_onboarding_name_prompt_card(
                role_name=ROLE_DISPLAY[role_code],
            ),
            card_type="onboarding_role",
            detail="employee onboarding name prompt",
        )

    async def _handle_name_input(self, event: AstrMessageEvent, name_text: str) -> None:
        """收到员工的姓名文字消息 → 存 + 自动进入培训第 1 节（带倒计时）。"""
        # 姓名简单清洗
        name = name_text.strip().replace("我叫", "").replace("我是", "").strip()
        if not name or len(name) > 20:
            await event.send(
                event.plain_result("请直接发你的姓名（中文 1-10 字），例：「您的姓名」")
            )
            return
        open_id = self._open_id(event)
        store = self._store()
        if not store:
            return
        await store.update_profile(open_id, display_name=name)
        await self._set_onboarding_state(open_id, stage="tutorial")
        # 直接推第 1 节教程卡（自动引导链模式），并启动 60 秒倒计时
        await self._push_lesson_and_arm_timer(event, FIRST_LESSON_ID)
        logger.info(
            "[onboarding] 员工 %s 姓名已存 → 自动进入培训 lesson=%s",
            name,
            FIRST_LESSON_ID,
        )

    async def _on_open_lesson(self, event: AstrMessageEvent, lesson_id: str) -> None:
        """点教程清单里的某节 → 推该节卡。

        - 入职引导链阶段（stage='tutorial'）：auto_progress 模式 + 倒计时
        - 复习模式（stage='quiz_failed'）：只推卡，不启动倒计时
        """
        if lesson_id not in TUTORIALS:
            return
        open_id = self._open_id(event)
        state = await self._get_onboarding_state(open_id)
        opened = list(state.get("opened_lessons") or [])
        if lesson_id not in opened:
            opened.append(lesson_id)
            await self._set_onboarding_state(open_id, opened_lessons=opened)
        stage = (state.get("stage") or "") if isinstance(state, dict) else ""
        dept_code = state.get("dept_code") if isinstance(state, dict) else None
        if stage == "tutorial":
            await self._push_lesson_and_arm_timer(event, lesson_id)
        else:
            # 复习模式（或其他兜底）：不启动倒计时
            self._cancel_lesson_timeout(open_id)
            card = build_tutorial_lesson_card(
                lesson_id=lesson_id, auto_progress=False, dept_code=dept_code
            )
            if card:
                await self._send_card(
                    event,
                    card,
                    card_type="training_lesson",
                    detail=f"employee onboarding review lesson: {lesson_id}",
                )

    async def _on_continue_tutorial(
        self, event: AstrMessageEvent, current_lesson_id: str
    ) -> None:
        """点「📖 继续学下一节」→ 找下一节推卡，重置倒计时。"""
        nid = next_lesson_id(current_lesson_id)
        open_id = self._open_id(event)
        if not nid:
            # 已经是最后一节但用户还点了 continue → 兜底进测试
            self._cancel_lesson_timeout(open_id)
            await self._on_start_quiz(event)
            return
        await self._push_lesson_and_arm_timer(event, nid)

    # ─────────────────── 倒计时机制 ───────────────────

    async def _push_lesson_and_arm_timer(
        self, event: AstrMessageEvent, lesson_id: str
    ) -> None:
        """推一张 lesson 卡（auto_progress 模式）+ 启动 60 秒倒计时 task。

        倒计时到了：有下一节 → 自动推下一节并继续 arm；最后一节 → 自动推第 1 题。
        点任何按钮（continue/start_quiz/select_dept 等）会 cancel 当前 task。

        lesson_dept_common 的内容靠 state.dept_code 渲染，所以这里要传过去。
        """
        open_id = self._open_id(event)
        # 标记 lesson 已 opened（供"答错时复习清单"用）
        state = await self._get_onboarding_state(open_id)
        opened = list(state.get("opened_lessons") or [])
        if lesson_id not in opened:
            opened.append(lesson_id)
            await self._set_onboarding_state(open_id, opened_lessons=opened)
        dept_code = state.get("dept_code") if isinstance(state, dict) else None
        card = build_tutorial_lesson_card(
            lesson_id=lesson_id,
            auto_progress=True,
            timeout_sec=LESSON_AUTO_ADVANCE_SEC,
            dept_code=dept_code,
        )
        if not card:
            return
        await self._send_card(
            event,
            card,
            card_type="training_lesson",
            detail=f"employee onboarding lesson: {lesson_id}",
        )
        self._schedule_lesson_timeout(event, lesson_id)

    def _schedule_lesson_timeout(
        self, event: AstrMessageEvent, current_lesson_id: str
    ) -> None:
        """启动 60 秒倒计时；超时后自动推下一节/启动测试。"""
        open_id = self._open_id(event)
        # 同一员工任何时候只允许一个倒计时 task
        self._cancel_lesson_timeout(open_id)
        task = asyncio.create_task(
            self._lesson_timeout_runner(event, current_lesson_id),
            name=f"lesson-timeout-{open_id[:12]}-{current_lesson_id}",
        )
        self._lesson_timeout_tasks[open_id] = task

    def _cancel_lesson_timeout(self, open_id: str) -> None:
        task = self._lesson_timeout_tasks.pop(open_id, None)
        if task and not task.done():
            task.cancel()

    async def _lesson_timeout_runner(
        self, event: AstrMessageEvent, current_lesson_id: str
    ) -> None:
        try:
            await asyncio.sleep(LESSON_AUTO_ADVANCE_SEC)
        except asyncio.CancelledError:
            return
        open_id = self._open_id(event)
        # 倒计时到了再核对一次 state：员工可能已经主动点了按钮进了别的 stage
        state = await self._get_onboarding_state(open_id)
        stage = (state.get("stage") or "") if isinstance(state, dict) else ""
        if stage != "tutorial":
            return  # 已经切到 quiz_active / done 等，不打扰
        nid = next_lesson_id(current_lesson_id)
        if nid:
            logger.info(
                "[onboarding] %s lesson=%s 60s 超时 → 自动推 %s",
                open_id[:12],
                current_lesson_id,
                nid,
            )
            await self._push_lesson_and_arm_timer(event, nid)
        else:
            logger.info(
                "[onboarding] %s lesson=%s 60s 超时（最后一节）→ 自动启动测试",
                open_id[:12],
                current_lesson_id,
            )
            await self._on_start_quiz(event)

    async def _on_show_tutorial_list(self, event: AstrMessageEvent) -> None:
        open_id = self._open_id(event)
        store = self._store()
        if not store:
            return
        emp = await store.get_employee(open_id)
        name = (emp.display_name if emp else "同学") or "同学"
        state = await self._get_onboarding_state(open_id)
        dept_code = state.get("dept_code") if isinstance(state, dict) else None
        await self._send_card(
            event,
            build_onboarding_tutorial_list_card(
                display_name=name,
                dept_code=dept_code,
            ),
            card_type="training_lesson",
            detail="employee onboarding tutorial list",
        )

    async def _quiz_questions_for(
        self, open_id: str
    ) -> tuple[list[dict[str, Any]], str | None]:
        """从 state 取 dept_code → 返回该员工应答题库（5 通用 + 1 部门差异）。

        dept_code 缺失 → 回退到通用 5 题，保证流程不卡。
        """
        state = await self._get_onboarding_state(open_id)
        dept_code = state.get("dept_code") if isinstance(state, dict) else None
        questions = get_quiz_for_dept(dept_code)
        return questions, dept_code

    async def _on_start_quiz(self, event: AstrMessageEvent) -> None:
        open_id = self._open_id(event)
        # 进入测试 → 取消任何还在跑的 lesson 倒计时
        self._cancel_lesson_timeout(open_id)
        questions, dept_code = await self._quiz_questions_for(open_id)
        total = len(questions)
        # 重置 quiz 状态：缓存 dept_code + total，防止答到一半被改部门串题
        await self._set_onboarding_state(
            open_id,
            stage="quiz_active",
            quiz_correct_count=0,
            quiz_answers={},
            quiz_started_at=self._now_iso(),
            quiz_total=total,
            quiz_dept_code=dept_code or "",
        )
        await self._send_card(
            event,
            build_quiz_question_card(
                q_num=1,
                total=total,
                questions=questions,
            ),
            card_type="training_quiz",
            detail="employee onboarding quiz question: 1",
        )

    async def _on_submit_quiz(
        self,
        event: AstrMessageEvent,
        q_num: int,
        choice: str,
    ) -> None:
        open_id = self._open_id(event)
        # 优先用 quiz_dept_code（quiz 开始时的快照）保证答题中途换部门不串题
        state = await self._get_onboarding_state(open_id)
        snapshot_dept = state.get("quiz_dept_code") if isinstance(state, dict) else None
        if snapshot_dept:
            questions = get_quiz_for_dept(snapshot_dept)
        else:
            questions, _ = await self._quiz_questions_for(open_id)
        total = len(questions)
        if q_num < 1 or q_num > total:
            return
        q = questions[q_num - 1]
        correct = choice == q["correct"]
        explain = q["explain"]

        # 更新状态
        answers = dict(state.get("quiz_answers", {}))
        answers[str(q_num)] = {
            "choice": choice,
            "correct": correct,
            "lesson_id": q.get("lesson_id", ""),
        }
        correct_count = sum(1 for a in answers.values() if a.get("correct"))
        missed_lessons = sorted(
            {
                str(a.get("lesson_id"))
                for a in answers.values()
                if not a.get("correct") and a.get("lesson_id")
            }
        )
        await self._set_onboarding_state(
            open_id,
            quiz_answers=answers,
            quiz_correct_count=correct_count,
            missed_lessons=missed_lessons,
        )

        next_q = q_num + 1 if q_num < total else None
        # 错题对应的 lesson_id，传给反馈卡渲染「返回复习再答」按钮
        wrong_lesson_id = q.get("lesson_id") if not correct else None
        # 发反馈卡：
        # - 答对最后一题：feedback（无按钮）→ 0.5s 后自动出结果
        # - 答错最后一题：feedback 含「返回复习再答 + 看结果」双按钮 → 不自动跳，等用户选
        # - 答对非最后题：feedback 含「继续下一题」单按钮
        # - 答错非最后题：feedback 含「返回复习再答 + 继续下一题」双按钮
        if next_q is None and correct:
            # 最后一题答对：自动出结果
            await self._send_card(
                event,
                build_quiz_feedback_card(
                    q_num=q_num,
                    correct=True,
                    explain=explain,
                    next_q=None,
                    total=total,
                ),
                card_type="training_quiz",
                detail=f"employee onboarding quiz feedback: {q_num}",
            )
            await asyncio.sleep(0.5)  # 让用户看清反馈
            await self._show_quiz_result(event)
        else:
            # 其他情况：feedback 卡自带按钮，不自动跳
            await self._send_card(
                event,
                build_quiz_feedback_card(
                    q_num=q_num,
                    correct=correct,
                    explain=explain,
                    next_q=next_q,
                    total=total,
                    lesson_id=wrong_lesson_id,
                ),
                card_type="training_quiz",
                detail=f"employee onboarding quiz feedback: {q_num}",
            )

    async def _on_next_quiz(self, event: AstrMessageEvent, q_num: int) -> None:
        open_id = self._open_id(event)
        state = await self._get_onboarding_state(open_id)
        snapshot_dept = state.get("quiz_dept_code") if isinstance(state, dict) else None
        questions = (
            get_quiz_for_dept(snapshot_dept)
            if snapshot_dept
            else (await self._quiz_questions_for(open_id))[0]
        )
        total = len(questions)
        if q_num < 1 or q_num > total:
            return
        await self._send_card(
            event,
            build_quiz_question_card(
                q_num=q_num,
                total=total,
                questions=questions,
            ),
            card_type="training_quiz",
            detail=f"employee onboarding quiz question: {q_num}",
        )

    async def _on_review_and_retry(
        self, event: AstrMessageEvent, q_num: int, lesson_id: str
    ) -> None:
        """答错→返回复习再答：推该题对应的 lesson 卡（retry 模式）。

        复习完员工点「✅ 已经复习好了，再答第 N 题」会触发 retry_quiz action。
        """
        open_id = self._open_id(event)
        # 切到 lesson 渲染，不再走倒计时（已经在 quiz_active 阶段）
        self._cancel_lesson_timeout(open_id)
        state = await self._get_onboarding_state(open_id)
        dept_code = state.get("dept_code") if isinstance(state, dict) else None
        if not lesson_id or lesson_id not in TUTORIALS:
            # lesson_id 缺失或未知 → 降级为直接重新出题
            await self._on_retry_quiz(event, q_num)
            return
        card = build_tutorial_lesson_card(
            lesson_id=lesson_id,
            auto_progress=False,
            dept_code=dept_code,
            retry_q_num=q_num,
        )
        if card:
            await self._send_card(
                event,
                card,
                card_type="training_lesson",
                detail=f"employee onboarding quiz review lesson: {lesson_id}",
            )

    async def _on_retry_quiz(self, event: AstrMessageEvent, q_num: int) -> None:
        """复习完点「再答第 N 题」→ 再次推该题（同一题），员工答完更新原 answers。"""
        open_id = self._open_id(event)
        state = await self._get_onboarding_state(open_id)
        snapshot_dept = state.get("quiz_dept_code") if isinstance(state, dict) else None
        questions = (
            get_quiz_for_dept(snapshot_dept)
            if snapshot_dept
            else (await self._quiz_questions_for(open_id))[0]
        )
        total = len(questions)
        if q_num < 1 or q_num > total:
            return
        await self._send_card(
            event,
            build_quiz_question_card(
                q_num=q_num,
                total=total,
                questions=questions,
            ),
            card_type="training_quiz",
            detail=f"employee onboarding quiz retry: {q_num}",
        )

    async def _on_show_result(self, event: AstrMessageEvent) -> None:
        """最后一题答错后点「📊 看结果」→ 直接进结果卡（接受当前成绩）。"""
        self._cancel_lesson_timeout(self._open_id(event))
        await self._show_quiz_result(event)

    async def _show_quiz_result(self, event: AstrMessageEvent) -> None:
        open_id = self._open_id(event)
        store = self._store()
        if not store:
            return
        emp = await store.get_employee(open_id)
        state = await self._get_onboarding_state(open_id)
        correct_count = state.get("quiz_correct_count", 0)
        # total 优先从 state.quiz_total 读（start_quiz 时快照），否则按 dept_code 回算
        total = state.get("quiz_total")
        if not isinstance(total, int) or total < 1:
            snapshot_dept = state.get("quiz_dept_code")
            total = len(
                get_quiz_for_dept(snapshot_dept) if snapshot_dept else QUIZ_QUESTIONS
            )
        # 全对才通过
        passed = correct_count >= total
        display_name = (emp.display_name if emp else "同学") or "同学"
        missed_lessons = list(state.get("missed_lessons") or [])
        invite_note = ""

        if passed:
            next_stage, invite_note, invite_error = await self._invite_after_pass(
                open_id
            )
            await self._set_onboarding_state(
                open_id,
                stage=next_stage,
                passed_at=self._now_iso(),
                invite_note=invite_note,
                invite_error=invite_error,
            )
            logger.info(
                "[onboarding] 员工 %s 测试通过 %s/%s stage=%s",
                display_name,
                correct_count,
                total,
                next_stage,
            )
        else:
            await self._set_onboarding_state(
                open_id,
                stage="quiz_failed",
                failed_at=self._now_iso(),
            )

        await self._send_card(
            event,
            build_quiz_result_card(
                display_name=display_name,
                correct_count=correct_count,
                total=total,
                invite_link=self._valid_invite_link() if passed else None,
                invite_note=invite_note,
                missed_lesson_ids=missed_lessons if not passed else None,
            ),
            card_type="training_quiz",
            detail="employee onboarding quiz result",
        )

    async def _invite_after_pass(self, open_id: str) -> tuple[str, str, str | None]:
        if self.auto_invite_to_chat and self.internal_test_chat_id:
            creator = self._chat_creator or ChatCreator()
            ok, invalid, err = await creator.invite_members(
                self.internal_test_chat_id,
                [open_id],
            )
            if err:
                note = "已通过测试，但自动拉群失败。请用下方链接进群，或等管理员处理。"
                return "invited" if self.invite_link else "passed", note, err
            if ok > 0 and open_id not in invalid:
                return "joined", "已通过测试，我已经自动把你拉进内测群。", None
            note = (
                "已通过测试，但飞书返回未成功入群。请用下方链接进群，或等管理员处理。"
            )
            return (
                "invited" if self._valid_invite_link() else "passed",
                note,
                "invalid_or_already_member",
            )
        if self._valid_invite_link():
            return "invited", "已通过测试，请点击下方入口加入内测群。", None
        return "passed", "已通过测试，请联系管理员邀请进群。", None

    # ─────────────────── Admin command ───────────────────

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command(
        "onboarding",
        desc="/onboarding status | scan | remind：查看或触发新人引导巡检",
    )
    async def onboarding_command(self, event: AstrMessageEvent):
        if not self._is_admin_event(event):
            self._reply(event, "权限不足：只有管理员可以使用 onboarding 命令。")
            return
        if not self._is_operational():
            self._reply(event, "Onboarding 当前处于关闭或维护模式。")
            return
        text = (event.message_str or "").strip()
        for prefix in ("/onboarding", "onboarding"):
            if text.startswith(prefix):
                text = text[len(prefix) :].strip()
                break
        sub = (text.split() or ["status"])[0].lower()
        store = self._store()
        if not store:
            self._reply(event, "⚠️ EmployeeStore 未初始化")
            return

        if sub == "status":
            employees = await store.list_employees(limit=self.scan_limit)
            counts: dict[str, int] = {}
            for emp in employees:
                stage = self._state_for(emp).get("stage") or "none"
                counts[str(stage)] = counts.get(str(stage), 0) + 1
            lines = [
                f"📋 Onboarding 状态（扫描 {len(employees)} 人）:",
                *[f"  • {stage}: {count}" for stage, count in sorted(counts.items())],
            ]
            self._reply(event, "\n".join(lines))
            return

        if sub in ("scan", "remind"):
            report = await self._run_outreach_scan(force=(sub == "remind"))
            self._reply(
                event,
                "✅ Onboarding 巡检完成：\n"
                f"  • 扫描：{report.get('scanned', 0)}\n"
                f"  • 符合触达：{report.get('eligible', 0)}\n"
                f"  • 已发送：{report.get('sent', 0)}\n"
                f"  • 失败：{report.get('failed', 0)}\n"
                f"  • 跳过：{report.get('skipped', 0)}",
            )
            return

        self._reply(
            event,
            "用法：\n"
            "  /onboarding status — 查看各阶段人数\n"
            "  /onboarding scan — 按冷却规则巡检并触达\n"
            "  /onboarding remind — 强制重新触达未完成员工",
        )
