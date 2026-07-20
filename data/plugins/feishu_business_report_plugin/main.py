"""Business report plugin for Feishu admin, HR, finance, and health snapshots."""

from __future__ import annotations

from pathlib import Path

from dc_engines.feishu_business_mvp import (
    BusinessMvpStore,
    build_default_runner,
    load_business_mvp_config,
    load_business_mvp_config_payload,
)
from dc_engines.feishu_hub import get_hub, is_enabled

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.message.message_event_result import MessageEventResult


@register(
    "feishu_business_report_plugin",
    "dc_agent",
    "飞书综合/财务报告：周报、月报、异常清单和集成健康",
    "0.1.0",
)
class FeishuBusinessReportPlugin(Star):
    """Expose business summary, exception report, and Feishu health APIs."""

    def __init__(self, context: Context, config=None) -> None:
        """Initialize plugin state.

        Args:
            context: AstrBot plugin context.
            config: Optional plugin configuration.
        """

        super().__init__(context)
        cfg = config or {}
        project_root = Path(__file__).resolve().parents[3]
        self.project_root = project_root
        self.enabled = bool(cfg.get("enabled", True))
        self.db_path = Path(
            cfg.get("db_path") or project_root / "data" / "feishu_business_mvp.db"
        )
        self.business_config_path = Path(
            cfg.get("business_config_path")
            or project_root / "data" / "config" / "feishu_business_mvp.json"
        )
        self.management_chat_id = str(cfg.get("management_chat_id") or "")
        self.store = BusinessMvpStore(self.db_path)

    async def initialize(self) -> None:
        """Initialize storage and register Web APIs.

        Returns:
            None.
        """

        await self.store.initialize()
        self.context.feishu_business_report_store = self.store
        try:
            self.context.register_web_api(
                "/feishu_business/summary",
                self._api_summary,
                ["GET"],
                "综合/财务/HR 业务摘要",
            )
            self.context.register_web_api(
                "/feishu_business/health",
                self._api_health,
                ["GET"],
                "飞书集成健康状态",
            )
            self.context.register_web_api(
                "/feishu_business/run",
                self._api_run,
                ["POST"],
                "运行飞书综合/财务同步、提醒或周报动作",
            )
            logger.info(
                "[feishu_business_report] API ready under /api/plug/feishu_business_report_plugin"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[feishu_business_report] register API failed: %s", exc)

    @filter.command("business-report", desc="飞书综合/财务业务摘要")
    async def business_report_command(self, event: AstrMessageEvent) -> None:
        """Reply with a compact weekly-style business report.

        Args:
            event: Message event.
        """

        snapshot = await self.store.snapshot()
        counts = snapshot["counts"]
        lines = [
            "飞书综合/财务摘要：",
            f"- 办公用品：{counts['asset_items']} 项，低库存 {len(snapshot['low_stock_items'])} 项",
            f"- 财务审批：{counts['finance_approval_records']} 单，缺附件 {len(snapshot['finance_missing_attachments'])} 单",
            f"- 入职待办：{counts['hr_onboarding_tasks']} 项，未完成 {len(snapshot['pending_onboarding_tasks'])} 项",
            f"- 飞书接口：{'启用' if is_enabled() else '未启用'}",
        ]
        self._reply(event, "\n".join(lines))

    async def _api_summary(self, *args, **kwargs):
        """Return the business summary used by dashboard pages.

        Returns:
            JSON-friendly business snapshot.
        """

        snapshot = await self.store.snapshot()
        return {
            "status": "ok",
            "message": None,
            "data": {
                "enabled": self.enabled,
                "management_chat_configured": bool(self.management_chat_id),
                **snapshot,
            },
        }

    async def _api_health(self, *args, **kwargs):
        """Return Feishu integration health.

        Returns:
            JSON-friendly Feishu hub health snapshot.
        """

        stats = get_hub().stats.snapshot()
        return {
            "status": "ok",
            "message": None,
            "data": {
                "enabled": self.enabled,
                "feishu_enabled": is_enabled(),
                "hub_stats": stats,
                "db_path": str(self.db_path),
                "management_chat_configured": bool(self.management_chat_id),
            },
        }

    async def _api_run(self, *args, **kwargs):
        """Run selected business workflow actions.

        Returns:
            JSON-friendly workflow result.
        """

        from astrbot.api.web import request

        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return {"status": "error", "message": "invalid json body", "data": None}
        raw_actions = payload.get("actions") or []
        actions = (
            [str(item) for item in raw_actions] if isinstance(raw_actions, list) else []
        )
        config = load_business_mvp_config(self.business_config_path)
        raw_config = load_business_mvp_config_payload(self.business_config_path)
        config.enabled = bool(payload.get("enabled", self.enabled))
        if not Path(config.db_path).is_absolute():
            config.db_path = self.project_root / config.db_path
        if self.db_path:
            config.db_path = self.db_path
        runner = build_default_runner(config)
        result = await runner.run_once(actions, raw_config=raw_config)
        return {"status": "ok", "message": None, "data": result}

    @staticmethod
    def _reply(event: AstrMessageEvent, text: str) -> None:
        """Send a plain text command reply.

        Args:
            event: Message event.
            text: Reply text.
        """

        event.set_result(MessageEventResult().message(text).use_t2i(False))
