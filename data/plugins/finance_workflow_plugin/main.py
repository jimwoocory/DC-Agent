"""Finance workflow plugin for Feishu approval exception handling."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from dc_engines.feishu_business_mvp import (
    ApprovalSyncer,
    BusinessMvpStore,
    FinanceApprovalRecord,
)
from dc_engines.feishu_business_mvp.approval import assess_attachment_status

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.message.message_event_result import MessageEventResult


@register(
    "finance_workflow_plugin",
    "dc_agent",
    "财务审批：报销/付款/采购审批同步、附件检查和台账",
    "0.1.0",
)
class FinanceWorkflowPlugin(Star):
    """Expose finance approval commands and Web APIs."""

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
        self.approval_codes = dict(cfg.get("approval_codes") or {})
        self.required_attachments = {
            str(key): [str(item) for item in value]
            for key, value in dict(cfg.get("required_attachments") or {}).items()
            if isinstance(value, list)
        }
        self.store = BusinessMvpStore(self.db_path)
        self.syncer = ApprovalSyncer(
            store=self.store,
            enabled=self.enabled,
            required_attachments=self.required_attachments,
        )

    async def initialize(self) -> None:
        """Initialize storage and register Web APIs.

        Returns:
            None.
        """

        await self.store.initialize()
        self.context.feishu_business_finance_store = self.store
        self.context.feishu_business_finance_syncer = self.syncer
        try:
            self.context.register_web_api(
                "/feishu_business/finance/records",
                self._api_records,
                ["GET"],
                "财务审批台账",
            )
            self.context.register_web_api(
                "/feishu_business/finance/missing-attachments",
                self._api_missing_attachments,
                ["GET"],
                "缺附件审批清单",
            )
            self.context.register_web_api(
                "/feishu_business/finance/sync",
                self._api_sync,
                ["POST"],
                "按 approval_code 同步审批实例",
            )
            logger.info(
                "[finance_workflow] API ready under /api/plug/finance_workflow_plugin"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[finance_workflow] register API failed: %s", exc)

    @filter.command("finance-summary", desc="财务审批摘要")
    async def finance_summary_command(self, event: AstrMessageEvent) -> None:
        """Reply with finance approval summary.

        Args:
            event: Message event.
        """

        records = await self.store.list_finance_records(limit=200)
        missing = [
            record for record in records if record.attachment_status == "missing"
        ]
        amount = sum(record.amount for record in records)
        self._reply(
            event,
            f"财务审批摘要：共 {len(records)} 单，金额合计 {amount:.2f}，缺附件 {len(missing)} 单。",
        )

    @filter.command("finance-missing", desc="财务缺附件清单")
    async def finance_missing_command(self, event: AstrMessageEvent) -> None:
        """Reply with finance approval records missing attachments.

        Args:
            event: Message event.
        """

        records = await self.store.list_finance_records(
            missing_attachments_only=True,
            limit=20,
        )
        if not records:
            self._reply(event, "当前没有缺附件的财务审批。")
            return
        lines = ["缺附件审批："]
        for record in records:
            missing = "、".join(record.missing_attachments) or "未标明"
            lines.append(
                f"- {record.approval_instance_code} {record.applicant_name} "
                f"{record.amount:.2f}: 缺 {missing}"
            )
        self._reply(event, "\n".join(lines))

    async def _api_records(self, *args, **kwargs):
        """Return finance approval records.

        Returns:
            JSON-friendly finance records.
        """

        from astrbot.api.web import request

        status = str(request.query.get("status", "") or "")
        records = await self.store.list_finance_records(status=status, limit=200)
        return {
            "status": "ok",
            "message": None,
            "data": {
                "enabled": self.enabled,
                "records": [
                    BusinessMvpStore._finance_record_to_dict(record)
                    for record in records
                ],
            },
        }

    async def _api_missing_attachments(self, *args, **kwargs):
        """Return finance records missing required attachments.

        Returns:
            JSON-friendly finance exception list.
        """

        records = await self.store.list_finance_records(
            missing_attachments_only=True,
            limit=200,
        )
        return {
            "status": "ok",
            "message": None,
            "data": {
                "enabled": self.enabled,
                "records": [
                    BusinessMvpStore._finance_record_to_dict(record)
                    for record in records
                ],
            },
        }

    async def _api_sync(self, *args, **kwargs):
        """Sync approval instances from Feishu approval API.

        Returns:
            JSON-friendly sync result.
        """

        from astrbot.api.web import request

        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return {"status": "error", "message": "invalid json body", "data": None}
        approval_type = str(payload.get("approval_type") or "reimbursement")
        approval_code = str(
            payload.get("approval_code") or self.approval_codes.get(approval_type) or ""
        )
        start_time = int(payload.get("start_time") or 0)
        end_time = int(payload.get("end_time") or 0)
        if not approval_code or not start_time or not end_time:
            return {
                "status": "error",
                "message": "approval_code/start_time/end_time required",
                "data": None,
            }
        records = await self.syncer.sync_finance_approvals(
            approval_code,
            approval_type,
            start_time=start_time,
            end_time=end_time,
        )
        return {
            "status": "ok",
            "message": None,
            "data": {
                "enabled": self.syncer.enabled,
                "synced": len(records),
                "records": [
                    BusinessMvpStore._finance_record_to_dict(record)
                    for record in records
                ],
            },
        }

    @staticmethod
    def build_record_from_fields(
        *,
        approval_instance_code: str,
        approval_type: str,
        fields: dict[str, Any],
        required_attachments: list[str],
    ) -> FinanceApprovalRecord:
        """Build a finance record from already-normalized fields.

        Args:
            approval_instance_code: Approval instance code.
            approval_type: Approval type.
            fields: Normalized approval fields.
            required_attachments: Required attachment labels.

        Returns:
            FinanceApprovalRecord.
        """

        attachment_status, missing = assess_attachment_status(
            fields,
            required_attachments,
        )
        return FinanceApprovalRecord(
            approval_instance_code=approval_instance_code,
            approval_type=approval_type,
            status=str(fields.get("状态") or fields.get("status") or "unknown"),
            attachment_status=attachment_status,
            missing_attachments=missing,
            metadata={"fields": fields},
        )

    @staticmethod
    def _reply(event: AstrMessageEvent, text: str) -> None:
        """Send a plain text command reply.

        Args:
            event: Message event.
            text: Reply text.
        """

        event.set_result(MessageEventResult().message(text).use_t2i(False))
