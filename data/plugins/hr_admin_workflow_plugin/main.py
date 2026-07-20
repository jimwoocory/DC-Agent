"""HR/admin workflow plugin for Feishu business MVP onboarding tasks."""

from __future__ import annotations

import uuid
from pathlib import Path

from dc_engines.feishu_business_mvp import BusinessMvpStore, HrOnboardingTask

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.message.message_event_result import MessageEventResult


@register(
    "hr_admin_workflow_plugin",
    "dc_agent",
    "综合人事：入职待办、面试协同和员工资料问答台账",
    "0.1.0",
)
class HrAdminWorkflowPlugin(Star):
    """Expose HR onboarding commands and Web APIs."""

    def __init__(self, context: Context, config=None) -> None:
        """Initialize plugin state.

        Args:
            context: AstrBot plugin context.
            config: Optional plugin configuration.
        """

        super().__init__(context)
        cfg = config or {}
        project_root = Path(__file__).resolve().parents[3]
        self.enabled = bool(cfg.get("enabled", True))
        self.db_path = Path(
            cfg.get("db_path") or project_root / "data" / "feishu_business_mvp.db"
        )
        self.default_tasks = [
            str(item)
            for item in cfg.get(
                "default_tasks",
                ["账号开通", "办公用品准备", "工位确认", "入职资料收集"],
            )
        ]
        self.store = BusinessMvpStore(self.db_path)

    async def initialize(self) -> None:
        """Initialize storage and register Web APIs.

        Returns:
            None.
        """

        await self.store.initialize()
        self.context.feishu_business_hr_store = self.store
        try:
            self.context.register_web_api(
                "/feishu_business/hr/onboarding",
                self._api_onboarding,
                ["GET"],
                "HR 入职待办列表",
            )
            self.context.register_web_api(
                "/feishu_business/hr/onboarding",
                self._api_onboarding_create,
                ["POST"],
                "HR 新增入职待办",
            )
            logger.info(
                "[hr_admin_workflow] API ready under /api/plug/hr_admin_workflow_plugin"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[hr_admin_workflow] register API failed: %s", exc)

    @filter.command("onboarding-list", desc="入职待办列表")
    async def onboarding_list_command(self, event: AstrMessageEvent) -> None:
        """Reply with open onboarding tasks.

        Args:
            event: Message event.
        """

        tasks = await self.store.list_onboarding_tasks(open_only=True, limit=20)
        if not tasks:
            self._reply(event, "当前没有待处理入职任务。")
            return
        lines = ["入职待办："]
        for task in tasks:
            lines.append(
                f"- {task.employee_name} / {task.task_name} / {task.owner_name or task.owner_id} / {task.status}"
            )
        self._reply(event, "\n".join(lines))

    @filter.command(
        "onboarding-add",
        desc="新增入职待办：/onboarding-add <姓名> <部门> <负责人> <待办>",
    )
    async def onboarding_add_command(self, event: AstrMessageEvent) -> None:
        """Create one onboarding task from chat.

        Args:
            event: Message event.
        """

        args = (
            (event.message_str or "").replace("/onboarding-add", "", 1).strip().split()
        )
        if len(args) < 4:
            self._reply(event, "用法：/onboarding-add <姓名> <部门> <负责人> <待办>")
            return
        task = await self.store.upsert_onboarding_task(
            HrOnboardingTask(
                task_id=uuid.uuid4().hex,
                employee_id=args[0],
                employee_name=args[0],
                department=args[1],
                owner_id=args[2],
                owner_name=args[2],
                task_name=" ".join(args[3:]),
            )
        )
        self._reply(event, f"已新增入职待办：{task.employee_name} / {task.task_name}")

    async def _api_onboarding(self, *args, **kwargs):
        """Return HR onboarding tasks.

        Returns:
            JSON-friendly onboarding task payload.
        """

        from astrbot.api.web import request

        status = str(request.query.get("status", "") or "")
        employee_id = str(request.query.get("employee_id", "") or "")
        open_only = str(request.query.get("open_only", "") or "").lower() in {
            "1",
            "true",
            "yes",
        }
        tasks = await self.store.list_onboarding_tasks(
            status=status,
            employee_id=employee_id,
            open_only=open_only,
            limit=200,
        )
        return {
            "status": "ok",
            "message": None,
            "data": {
                "enabled": self.enabled,
                "tasks": [
                    BusinessMvpStore._onboarding_task_to_dict(task) for task in tasks
                ],
            },
        }

    async def _api_onboarding_create(self, *args, **kwargs):
        """Create onboarding tasks from Dashboard or Feishu table sync.

        Returns:
            JSON-friendly created task payload.
        """

        from astrbot.api.web import request

        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return {"status": "error", "message": "invalid json body", "data": None}
        employee_name = str(payload.get("employee_name") or "").strip()
        if not employee_name:
            return {
                "status": "error",
                "message": "employee_name required",
                "data": None,
            }
        task_names = payload.get("task_names")
        if not isinstance(task_names, list) or not task_names:
            task_names = self.default_tasks
        created: list[dict] = []
        for task_name in task_names:
            task = await self.store.upsert_onboarding_task(
                HrOnboardingTask(
                    task_id=str(payload.get("task_id") or uuid.uuid4().hex),
                    employee_id=str(payload.get("employee_id") or employee_name),
                    employee_name=employee_name,
                    department=str(payload.get("department") or ""),
                    owner_id=str(payload.get("owner_id") or ""),
                    owner_name=str(payload.get("owner_name") or ""),
                    task_name=str(task_name),
                    due_at=str(payload.get("due_at") or ""),
                    metadata=dict(payload.get("metadata") or {}),
                )
            )
            created.append(BusinessMvpStore._onboarding_task_to_dict(task))
        return {"status": "ok", "message": None, "data": {"created": created}}

    @staticmethod
    def _reply(event: AstrMessageEvent, text: str) -> None:
        """Send a plain text command reply.

        Args:
            event: Message event.
            text: Reply text.
        """

        event.set_result(MessageEventResult().message(text).use_t2i(False))
