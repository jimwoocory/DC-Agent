"""Data contracts for the Feishu business MVP."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ASSET_OUTBOUND_MOVEMENTS = {"claim", "borrow", "consume"}
ASSET_INBOUND_MOVEMENTS = {"return", "purchase", "restock"}
ASSET_SET_MOVEMENTS = {"inventory_adjust"}
OPEN_ONBOARDING_STATUSES = {"pending", "in_progress", "overdue"}


@dataclass(slots=True)
class BitableLocation:
    """Location of a Feishu Bitable table used by a business workflow.

    Args:
        app_token: Feishu Bitable app token.
        table_id: Table ID inside the Bitable app.
        view_id: Optional view ID used by read/search calls.
        primary_key: Optional business primary key field.
    """

    app_token: str = ""
    table_id: str = ""
    view_id: str = ""
    primary_key: str = ""

    @property
    def configured(self) -> bool:
        """Return whether the table has enough IDs for API calls.

        Returns:
            True when app_token and table_id are both non-empty.
        """

        return bool(self.app_token and self.table_id)


@dataclass(slots=True)
class BusinessMvpConfig:
    """Runtime configuration for the Feishu business MVP.

    Args:
        enabled: Whether business workflows are enabled.
        db_path: Local SQLite path for cache and audit records.
        asset_table: Feishu Bitable location for asset inventory.
        finance_table: Feishu Bitable location for finance approvals.
        onboarding_table: Feishu Bitable location for onboarding tasks.
        approval_codes: Mapping of workflow type to approval code.
        notification_targets: Mapping of business target name to open_id/chat_id.
        required_finance_attachments: Required attachment labels per approval type.
        default_onboarding_tasks: Tasks generated for each new employee row.
        sync_window_hours: Default approval sync lookback window.
    """

    enabled: bool = True
    db_path: str | Path = "data/feishu_business_mvp.db"
    asset_table: BitableLocation = field(default_factory=BitableLocation)
    finance_table: BitableLocation = field(default_factory=BitableLocation)
    onboarding_table: BitableLocation = field(default_factory=BitableLocation)
    approval_codes: dict[str, str] = field(default_factory=dict)
    notification_targets: dict[str, str] = field(default_factory=dict)
    required_finance_attachments: dict[str, list[str]] = field(default_factory=dict)
    default_onboarding_tasks: list[str] = field(
        default_factory=lambda: ["账号开通", "办公用品准备", "工位确认", "入职资料收集"]
    )
    sync_window_hours: int = 168


@dataclass(slots=True)
class AssetItem:
    """Administrative asset inventory item.

    Args:
        item_id: Stable item ID.
        name: Item display name.
        category: Item category.
        stock: Current stock quantity.
        warning_threshold: Low-stock warning threshold.
        custodian: Person or department responsible for the item.
        status: Lifecycle status such as active or archived.
        updated_at: UTC ISO timestamp.
        metadata: Extra source fields from Feishu or integrations.
    """

    item_id: str
    name: str
    category: str = ""
    stock: int = 0
    warning_threshold: int = 0
    custodian: str = ""
    status: str = "active"
    updated_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def low_stock(self) -> bool:
        """Return whether current stock is at or below the warning line.

        Returns:
            True when the item is active and stock is below threshold.
        """

        return (
            self.status == "active"
            and self.warning_threshold > 0
            and (self.stock <= self.warning_threshold)
        )


@dataclass(slots=True)
class AssetMovement:
    """Inventory movement for claims, returns, purchases, or stock counts.

    Args:
        movement_id: Stable movement ID.
        item_id: Target asset item ID.
        movement_type: One of claim, borrow, consume, return, purchase, restock,
            or inventory_adjust.
        quantity: Positive quantity. inventory_adjust sets stock to this value.
        actor_id: User open_id/user_id or internal actor ID.
        actor_name: Actor display name.
        note: Optional movement note.
        created_at: UTC ISO timestamp.
        metadata: Extra source fields from cards or Bitable records.
    """

    movement_id: str
    item_id: str
    movement_type: str
    quantity: int
    actor_id: str = ""
    actor_name: str = ""
    note: str = ""
    created_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class FinanceApprovalRecord:
    """Finance approval cache row.

    Args:
        approval_instance_code: Feishu approval instance code or ID.
        approval_type: Approval type such as reimbursement, payment, purchase,
            or contract.
        amount: Approval amount visible to finance/management roles.
        department: Applicant department.
        applicant_id: Applicant open_id/user_id.
        applicant_name: Applicant display name.
        status: Approval status from Feishu or local workflow.
        attachment_status: complete, missing, or unknown.
        missing_attachments: Required attachment labels that are missing.
        approved_at: UTC ISO approval time when available.
        source_record_id: Linked Bitable record ID.
        updated_at: UTC ISO timestamp.
        metadata: Extra normalized approval fields.
    """

    approval_instance_code: str
    approval_type: str
    amount: float = 0.0
    department: str = ""
    applicant_id: str = ""
    applicant_name: str = ""
    status: str = "unknown"
    attachment_status: str = "unknown"
    missing_attachments: list[str] = field(default_factory=list)
    approved_at: str = ""
    source_record_id: str = ""
    updated_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class HrOnboardingTask:
    """HR and admin onboarding task.

    Args:
        task_id: Stable onboarding task ID.
        employee_id: New employee open_id/user_id or internal ID.
        employee_name: New employee display name.
        department: Target department.
        owner_id: Task owner open_id/user_id.
        owner_name: Task owner display name.
        task_name: Human-readable task name.
        status: pending, in_progress, done, skipped, or overdue.
        due_at: UTC ISO due timestamp.
        completed_at: UTC ISO completion timestamp.
        updated_at: UTC ISO update timestamp.
        metadata: Extra source fields from HR records.
    """

    task_id: str
    employee_id: str
    employee_name: str
    department: str = ""
    owner_id: str = ""
    owner_name: str = ""
    task_name: str = ""
    status: str = "pending"
    due_at: str = ""
    completed_at: str = ""
    updated_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class NotificationLog:
    """Business notification audit row.

    Args:
        notification_id: Stable notification ID.
        target_type: open_id, user_id, email, or chat_id.
        target_id: Recipient ID.
        notification_type: Business notification type.
        send_status: sent, disabled, skipped, or failed.
        fail_reason: Failure reason when send_status is failed.
        created_at: UTC ISO timestamp.
        payload: Sent message or card summary.
    """

    notification_id: str
    target_type: str
    target_id: str
    notification_type: str
    send_status: str
    fail_reason: str = ""
    created_at: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
