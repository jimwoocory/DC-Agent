"""Runnable Feishu business MVP synchronization and reminder workflows."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

from .approval import ApprovalSyncer
from .bitable import FeishuBitableClient
from .config import validate_business_mvp_config
from .contracts import (
    AssetItem,
    BusinessMvpConfig,
    FinanceApprovalRecord,
    HrOnboardingTask,
)
from .notifications import BusinessNotifier
from .store import BusinessMvpStore


class BusinessWorkflowRunner:
    """Coordinate Bitable sync, approval sync, notifications, and reports."""

    def __init__(
        self,
        *,
        config: BusinessMvpConfig,
        store: BusinessMvpStore,
        bitable: FeishuBitableClient,
        approval_syncer: ApprovalSyncer,
        notifier: BusinessNotifier,
    ) -> None:
        """Create a workflow runner.

        Args:
            config: Business workflow config.
            store: Local business store.
            bitable: Feishu Bitable adapter.
            approval_syncer: Feishu approval sync adapter.
            notifier: Feishu business notifier.
        """

        self.config = config
        self.store = store
        self.bitable = bitable
        self.approval_syncer = approval_syncer
        self.notifier = notifier

    async def sync_assets_from_bitable(self) -> dict[str, Any]:
        """Sync asset inventory rows from Feishu Bitable into SQLite.

        Returns:
            Sync result with count and enabled/configured flags.
        """

        if not self.config.enabled or not self.config.asset_table.configured:
            return {"enabled": self.config.enabled, "configured": False, "synced": 0}
        records = await self.bitable.list_records(self.config.asset_table, limit=1000)
        synced = 0
        for record in records:
            fields = record.fields
            name = _field(fields, ["物品名称", "名称", "name"])
            if not name:
                continue
            item_id = (
                _field(
                    fields,
                    [self.config.asset_table.primary_key, "物品ID", "item_id", "编号"],
                )
                or record.record_id
            )
            await self.store.upsert_asset_item(
                AssetItem(
                    item_id=item_id,
                    name=name,
                    category=_field(fields, ["类别", "category"]),
                    stock=_int_field(fields, ["库存", "stock", "数量"]),
                    warning_threshold=_int_field(
                        fields, ["预警线", "warning_threshold", "预警库存"]
                    ),
                    custodian=_field(fields, ["保管人", "custodian", "负责人"]),
                    status=_field(fields, ["状态", "status"]) or "active",
                    metadata={"source_record_id": record.record_id, "fields": fields},
                )
            )
            synced += 1
        return {
            "enabled": self.config.enabled,
            "configured": True,
            "feishu_enabled": self.bitable.enabled,
            "synced": synced,
        }

    async def preflight(
        self, raw_config: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Check config and runtime readiness without writing business rows.

        Args:
            raw_config: Optional raw config payload used to detect forbidden secrets.

        Returns:
            Preflight status, issues, and component readiness.
        """

        issues = validate_business_mvp_config(self.config, raw_config)
        if not self.config.enabled:
            issues.append(
                {
                    "severity": "warning",
                    "code": "business_mvp_disabled",
                    "path": "enabled",
                    "message": "业务配置 enabled=false，runner 会走禁用/跳过分支。",
                }
            )
        if not self.bitable.enabled:
            issues.append(
                {
                    "severity": "warning",
                    "code": "feishu_hub_disabled",
                    "path": "feishu_hub",
                    "message": "飞书 hub 当前不可用，真实 API 同步和消息发送会跳过或记 disabled。",
                }
            )
        severities = {issue["severity"] for issue in issues}
        status = (
            "blocked" if "error" in severities else "degraded" if issues else "ready"
        )
        return {
            "status": status,
            "issues": issues,
            "components": {
                "business_enabled": self.config.enabled,
                "feishu_bitable_enabled": self.bitable.enabled,
                "feishu_approval_enabled": self.approval_syncer.enabled,
                "feishu_notification_enabled": bool(
                    getattr(self.notifier, "enabled", True)
                ),
                "asset_table_configured": self.config.asset_table.configured,
                "onboarding_table_configured": self.config.onboarding_table.configured,
                "approval_code_count": len(self.config.approval_codes),
                "notification_target_count": len(self.config.notification_targets),
                "db_path": str(self.config.db_path),
            },
        }

    async def sync_onboarding_from_bitable(self) -> dict[str, Any]:
        """Sync onboarding rows and generate default onboarding tasks.

        Returns:
            Sync result with generated task count.
        """

        if not self.config.enabled or not self.config.onboarding_table.configured:
            return {"enabled": self.config.enabled, "configured": False, "synced": 0}
        records = await self.bitable.list_records(
            self.config.onboarding_table,
            limit=1000,
        )
        synced = 0
        for record in records:
            fields = record.fields
            employee_name = _field(fields, ["新人", "姓名", "employee_name", "name"])
            if not employee_name:
                continue
            employee_id = (
                _field(
                    fields,
                    ["open_id", "员工ID", "employee_id", "邮箱", "email"],
                )
                or employee_name
            )
            task_names = _list_field(fields, ["待办项", "任务", "tasks"])
            if not task_names:
                task_names = self.config.default_onboarding_tasks
            for task_name in task_names:
                task_id = _stable_id("onboarding", employee_id, task_name)
                await self.store.upsert_onboarding_task(
                    HrOnboardingTask(
                        task_id=task_id,
                        employee_id=employee_id,
                        employee_name=employee_name,
                        department=_field(fields, ["部门", "department"]),
                        owner_id=_field(fields, ["负责人ID", "owner_id", "主管ID"]),
                        owner_name=_field(fields, ["负责人", "主管", "owner_name"]),
                        task_name=task_name,
                        due_at=_field(fields, ["截止时间", "due_at", "deadline"]),
                        metadata={
                            "source_record_id": record.record_id,
                            "fields": fields,
                        },
                    )
                )
                synced += 1
        return {
            "enabled": self.config.enabled,
            "configured": True,
            "feishu_enabled": self.bitable.enabled,
            "synced": synced,
        }

    async def sync_finance_approvals(self) -> dict[str, Any]:
        """Sync configured Feishu approval codes into finance records.

        Returns:
            Sync result grouped by approval type.
        """

        if not self.config.enabled or not self.config.approval_codes:
            return {"enabled": self.config.enabled, "configured": False, "synced": 0}
        end_time = int(datetime.now(timezone.utc).timestamp() * 1000)
        start_time = int(
            (
                datetime.now(timezone.utc)
                - timedelta(hours=self.config.sync_window_hours)
            ).timestamp()
            * 1000
        )
        by_type: dict[str, int] = {}
        review_notifications = 0
        target = self.config.notification_targets.get(
            "finance_open_id"
        ) or self.config.notification_targets.get("finance_chat_id", "")
        notification_logs = await self.store.list_notification_logs(limit=1000)
        notified_review_messages = {
            str(log.payload.get("text") or "")
            for log in notification_logs
            if log.notification_type == "finance_invoice_manual_review"
            and log.send_status == "sent"
        }
        for approval_type, approval_code in self.config.approval_codes.items():
            records = await self.approval_syncer.sync_finance_approvals(
                approval_code,
                approval_type,
                start_time=start_time,
                end_time=end_time,
            )
            for record in records:
                review = dict(record.metadata.get("invoice_review") or {})
                reason_text = "、".join(review.get("reasons") or [])
                message = (
                    f"【报账机器人待复核】\n单号：{record.approval_instance_code}\n"
                    f"报销总额：{record.amount:.2f} 元\n"
                    f"发票 OCR 合计：{review.get('invoice_total')} 元\n"
                    f"复核原因：{reason_text}\n"
                    "机器人未自动同意或驳回，请核对附件与报销范围。"
                )
                if (
                    review.get("status") != "manual_review"
                    or not target
                    or any(
                        record.approval_instance_code in sent and reason_text in sent
                        for sent in notified_review_messages
                    )
                ):
                    continue
                await self.notifier.send_text(
                    target_id=target,
                    receive_id_type=_receive_id_type(target),
                    notification_type="finance_invoice_manual_review",
                    text=message,
                )
                notified_review_messages.add(message)
                review_notifications += 1
            by_type[approval_type] = len(records)
        return {
            "enabled": self.config.enabled,
            "configured": True,
            "feishu_enabled": self.approval_syncer.enabled,
            "synced": sum(by_type.values()),
            "by_type": by_type,
            "review_notifications": review_notifications,
        }

    async def notify_low_stock(self) -> dict[str, Any]:
        """Notify the admin target about low-stock asset items.

        Returns:
            Notification result with count and status.
        """

        items = await self.store.list_asset_items(low_stock_only=True, limit=50)
        target = self.config.notification_targets.get(
            "admin_chat_id"
        ) or self.config.notification_targets.get("admin_open_id", "")
        if not items:
            return {"sent": 0, "reason": "no_low_stock"}
        text = "低库存办公用品：\n" + "\n".join(
            f"- {item.name}: {item.stock}/{item.warning_threshold}" for item in items
        )
        log = await self.notifier.send_text(
            target_id=target,
            receive_id_type=_receive_id_type(target),
            text=text,
            notification_type="asset_low_stock",
        )
        return {"sent": 1, "send_status": log.send_status, "count": len(items)}

    async def notify_finance_missing_attachments(self) -> dict[str, Any]:
        """Notify finance/applicants about approval records missing attachments.

        Returns:
            Notification result with sent count.
        """

        records = await self.store.list_finance_records(
            missing_attachments_only=True,
            limit=50,
        )
        sent = 0
        for record in records:
            if record.applicant_id:
                log = await self.notifier.send_text(
                    target_id=record.applicant_id,
                    receive_id_type="open_id",
                    text=_finance_missing_text(record),
                    notification_type="finance_missing_attachment_applicant",
                )
                sent += 1 if log.send_status in {"sent", "disabled"} else 0
        finance_target = self.config.notification_targets.get(
            "finance_chat_id",
            "",
        )
        if finance_target and records:
            summary = "财务缺附件审批：\n" + "\n".join(
                f"- {record.approval_instance_code} {record.applicant_name}: "
                f"{'、'.join(record.missing_attachments)}"
                for record in records
            )
            log = await self.notifier.send_text(
                target_id=finance_target,
                receive_id_type=_receive_id_type(finance_target),
                text=summary,
                notification_type="finance_missing_attachment_summary",
            )
            sent += 1 if log.send_status in {"sent", "disabled"} else 0
        return {"records": len(records), "sent": sent}

    async def send_weekly_report(self) -> dict[str, Any]:
        """Send a weekly business report to the configured management target.

        Returns:
            Notification result and summary counts.
        """

        snapshot = await self.store.snapshot()
        target = self.config.notification_targets.get(
            "management_chat_id",
            "",
        )
        counts = snapshot["counts"]
        text = "\n".join(
            [
                "飞书综合/财务周报",
                f"- 办公用品：{counts['asset_items']} 项，低库存 {len(snapshot['low_stock_items'])} 项",
                f"- 财务审批：{counts['finance_approval_records']} 单，缺附件 {len(snapshot['finance_missing_attachments'])} 单",
                f"- 入职待办：{counts['hr_onboarding_tasks']} 项，未完成 {len(snapshot['pending_onboarding_tasks'])} 项",
            ]
        )
        log = await self.notifier.send_text(
            target_id=target,
            receive_id_type=_receive_id_type(target),
            text=text,
            notification_type="business_weekly_report",
        )
        return {"send_status": log.send_status, "counts": counts}

    async def run_once(
        self,
        actions: list[str],
        *,
        raw_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run selected workflow actions.

        Args:
            actions: Workflow action names.
            raw_config: Optional raw config payload used by preflight.

        Returns:
            Results keyed by action name.
        """

        await self.store.initialize()
        selected = actions or ["preflight"]
        result: dict[str, Any] = {}
        for action in selected:
            if action == "preflight":
                result[action] = await self.preflight(raw_config)
            elif action == "sync-assets":
                result[action] = await self.sync_assets_from_bitable()
            elif action == "sync-onboarding":
                result[action] = await self.sync_onboarding_from_bitable()
            elif action == "sync-finance":
                result[action] = await self.sync_finance_approvals()
            elif action == "notify-exceptions":
                result["notify-low-stock"] = await self.notify_low_stock()
                result[
                    "notify-finance"
                ] = await self.notify_finance_missing_attachments()
            elif action == "weekly-report":
                result[action] = await self.send_weekly_report()
            else:
                result[action] = {"error": "unknown action"}
        result["snapshot"] = await self.store.snapshot()
        return result


def build_default_runner(config: BusinessMvpConfig) -> BusinessWorkflowRunner:
    """Build a production runner using Feishu hub-backed adapters.

    Args:
        config: Business workflow config.

    Returns:
        BusinessWorkflowRunner.
    """

    store = BusinessMvpStore(config.db_path)
    return BusinessWorkflowRunner(
        config=config,
        store=store,
        bitable=FeishuBitableClient(enabled=config.enabled),
        approval_syncer=ApprovalSyncer(
            store=store,
            enabled=config.enabled,
            required_attachments=config.required_finance_attachments,
        ),
        notifier=BusinessNotifier(store=store, enabled=config.enabled),
    )


def _field(fields: dict[str, Any], names: list[str]) -> str:
    """Return the first non-empty field by fuzzy key names.

    Args:
        fields: Source fields.
        names: Preferred field names or fragments.

    Returns:
        First field value as text.
    """

    needles = [name.lower() for name in names if name]
    for key, value in fields.items():
        key_text = str(key).lower()
        if any(needle and needle in key_text for needle in needles):
            text = _stringify(value)
            if text:
                return text
    return ""


def _int_field(fields: dict[str, Any], names: list[str]) -> int:
    """Return the first matching field parsed as int.

    Args:
        fields: Source fields.
        names: Preferred field names.

    Returns:
        Parsed int or 0.
    """

    raw = _field(fields, names)
    digits = "".join(ch for ch in raw if ch.isdigit() or ch == "-")
    try:
        return int(digits) if digits else 0
    except ValueError:
        return 0


def _list_field(fields: dict[str, Any], names: list[str]) -> list[str]:
    """Return a matching field as a list of strings.

    Args:
        fields: Source fields.
        names: Preferred field names.

    Returns:
        String list.
    """

    needles = [name.lower() for name in names if name]
    for key, value in fields.items():
        key_text = str(key).lower()
        if not any(needle and needle in key_text for needle in needles):
            continue
        if isinstance(value, list):
            return [_stringify(item) for item in value if _stringify(item)]
        text = _stringify(value)
        if not text:
            return []
        return [
            part.strip() for part in text.replace("，", ",").split(",") if part.strip()
        ]
    return []


def _stringify(value: Any) -> str:
    """Flatten a Bitable field value into text.

    Args:
        value: Any field value.

    Returns:
        Text value.
    """

    if value is None:
        return ""
    if isinstance(value, str | int | float | bool):
        return str(value).strip()
    if isinstance(value, list):
        return "、".join(_stringify(item) for item in value if _stringify(item))
    if isinstance(value, dict):
        for key in ("text", "name", "email", "value", "link"):
            if key in value:
                text = _stringify(value[key])
                if text:
                    return text
        return " ".join(_stringify(item) for item in value.values() if _stringify(item))
    return str(value).strip()


def _stable_id(*parts: str) -> str:
    """Build a stable short ID from business keys.

    Args:
        *parts: ID parts.

    Returns:
        Stable ID string.
    """

    raw = "|".join(parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def _receive_id_type(target: str) -> str:
    """Infer Feishu receive_id_type from an ID prefix.

    Args:
        target: Feishu recipient ID.

    Returns:
        chat_id for group chats, otherwise open_id.
    """

    return "chat_id" if target.startswith("oc_") else "open_id"


def _finance_missing_text(record: FinanceApprovalRecord) -> str:
    """Build applicant-facing missing attachment text.

    Args:
        record: Finance approval record.

    Returns:
        Reminder text.
    """

    missing = "、".join(record.missing_attachments) or "必要材料"
    return (
        f"你的{record.approval_type}审批 {record.approval_instance_code} "
        f"缺少：{missing}。请补齐后通知财务复核。"
    )
