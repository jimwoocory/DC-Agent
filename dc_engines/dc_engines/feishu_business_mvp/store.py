"""SQLite store for Feishu business MVP cache and audit data."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from .contracts import (
    ASSET_INBOUND_MOVEMENTS,
    ASSET_OUTBOUND_MOVEMENTS,
    ASSET_SET_MOVEMENTS,
    OPEN_ONBOARDING_STATUSES,
    AssetItem,
    AssetMovement,
    FinanceApprovalRecord,
    HrOnboardingTask,
    NotificationLog,
)


class BusinessMvpStore:
    """SQLite persistence for the Feishu admin, HR, and finance MVP."""

    def __init__(self, db_path: str | Path) -> None:
        """Create a store instance.

        Args:
            db_path: SQLite database path.
        """

        self.db_path = str(db_path)
        self._initialized = False

    async def initialize(self) -> None:
        """Create required tables and indexes.

        Returns:
            None.
        """

        if self._initialized:
            return

        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA synchronous=NORMAL")
            await db.executescript("""
                CREATE TABLE IF NOT EXISTS asset_items (
                    item_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    category TEXT NOT NULL,
                    stock INTEGER NOT NULL,
                    warning_threshold INTEGER NOT NULL,
                    custodian TEXT NOT NULL,
                    status TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS asset_movements (
                    movement_id TEXT PRIMARY KEY,
                    item_id TEXT NOT NULL,
                    movement_type TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    actor_id TEXT NOT NULL,
                    actor_name TEXT NOT NULL,
                    note TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS finance_approval_records (
                    approval_instance_code TEXT PRIMARY KEY,
                    approval_type TEXT NOT NULL,
                    amount REAL NOT NULL,
                    department TEXT NOT NULL,
                    applicant_id TEXT NOT NULL,
                    applicant_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attachment_status TEXT NOT NULL,
                    missing_attachments_json TEXT NOT NULL,
                    approved_at TEXT NOT NULL,
                    source_record_id TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS hr_onboarding_tasks (
                    task_id TEXT PRIMARY KEY,
                    employee_id TEXT NOT NULL,
                    employee_name TEXT NOT NULL,
                    department TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    owner_name TEXT NOT NULL,
                    task_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    due_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS business_notification_logs (
                    notification_id TEXT PRIMARY KEY,
                    target_type TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    notification_type TEXT NOT NULL,
                    send_status TEXT NOT NULL,
                    fail_reason TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_asset_items_low_stock
                ON asset_items(status, stock, warning_threshold);

                CREATE INDEX IF NOT EXISTS idx_asset_movements_item
                ON asset_movements(item_id, created_at DESC);

                CREATE INDEX IF NOT EXISTS idx_finance_records_status
                ON finance_approval_records(status, updated_at DESC);

                CREATE INDEX IF NOT EXISTS idx_finance_records_attachments
                ON finance_approval_records(attachment_status, updated_at DESC);

                CREATE INDEX IF NOT EXISTS idx_onboarding_tasks_status
                ON hr_onboarding_tasks(status, due_at ASC);

                CREATE INDEX IF NOT EXISTS idx_notification_logs_recent
                ON business_notification_logs(created_at DESC);
            """)
            await db.commit()

        self._initialized = True

    async def upsert_asset_item(self, item: AssetItem) -> AssetItem:
        """Insert or update an asset item.

        Args:
            item: Asset item to persist.

        Returns:
            The persisted item with updated_at populated.
        """

        await self.initialize()
        next_item = AssetItem(
            item_id=item.item_id,
            name=item.name,
            category=item.category,
            stock=int(item.stock),
            warning_threshold=int(item.warning_threshold),
            custodian=item.custodian,
            status=item.status,
            updated_at=item.updated_at or self._utcnow(),
            metadata=dict(item.metadata),
        )
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO asset_items (
                    item_id, name, category, stock, warning_threshold, custodian,
                    status, metadata_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_id) DO UPDATE SET
                    name = excluded.name,
                    category = excluded.category,
                    stock = excluded.stock,
                    warning_threshold = excluded.warning_threshold,
                    custodian = excluded.custodian,
                    status = excluded.status,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (
                    next_item.item_id,
                    next_item.name,
                    next_item.category,
                    next_item.stock,
                    next_item.warning_threshold,
                    next_item.custodian,
                    next_item.status,
                    self._dumps(next_item.metadata),
                    next_item.updated_at,
                ),
            )
            await db.commit()
        return next_item

    async def get_asset_item(self, item_id: str) -> AssetItem | None:
        """Read one asset item by ID.

        Args:
            item_id: Asset item ID.

        Returns:
            Matching AssetItem or None.
        """

        await self.initialize()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM asset_items WHERE item_id = ?",
                (item_id,),
            )
            row = await cursor.fetchone()
        return self._asset_item_from_row(row) if row else None

    async def list_asset_items(
        self,
        *,
        low_stock_only: bool = False,
        limit: int = 200,
    ) -> list[AssetItem]:
        """List asset inventory rows.

        Args:
            low_stock_only: Whether to return only active low-stock rows.
            limit: Maximum rows to return.

        Returns:
            Asset rows ordered for dashboard scanning.
        """

        await self.initialize()
        query = "SELECT * FROM asset_items"
        params: list[Any] = []
        if low_stock_only:
            query += " WHERE status = 'active' AND warning_threshold > 0 AND stock <= warning_threshold"
        query += " ORDER BY category ASC, name ASC LIMIT ?"
        params.append(max(1, int(limit)))

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(query, params)
            rows = await cursor.fetchall()
        return [self._asset_item_from_row(row) for row in rows]

    async def apply_asset_movement(self, movement: AssetMovement) -> AssetItem:
        """Apply an inventory movement and write the audit row atomically.

        Args:
            movement: Inventory movement to apply.

        Returns:
            Updated AssetItem.

        Raises:
            LookupError: If the target item does not exist.
            ValueError: If movement type or quantity is invalid.
        """

        await self.initialize()
        movement_type = movement.movement_type.strip().lower()
        known_types = (
            ASSET_OUTBOUND_MOVEMENTS | ASSET_INBOUND_MOVEMENTS | ASSET_SET_MOVEMENTS
        )
        if movement_type not in known_types:
            raise ValueError(
                f"unsupported asset movement type: {movement.movement_type}"
            )
        if movement_type in ASSET_SET_MOVEMENTS:
            if movement.quantity < 0:
                raise ValueError(
                    "asset inventory adjustment quantity cannot be negative"
                )
        elif movement.quantity <= 0:
            raise ValueError("asset movement quantity must be positive")

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM asset_items WHERE item_id = ?",
                (movement.item_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                raise LookupError(f"asset item {movement.item_id!r} not found")

            item = self._asset_item_from_row(row)
            if movement_type in ASSET_OUTBOUND_MOVEMENTS:
                next_stock = item.stock - movement.quantity
                if next_stock < 0:
                    raise ValueError(
                        f"asset item {movement.item_id!r} stock would become negative"
                    )
            elif movement_type in ASSET_INBOUND_MOVEMENTS:
                next_stock = item.stock + movement.quantity
            else:
                next_stock = movement.quantity

            now = self._utcnow()
            movement_id = movement.movement_id or uuid.uuid4().hex
            created_at = movement.created_at or now
            await db.execute(
                """
                UPDATE asset_items
                SET stock = ?, updated_at = ?
                WHERE item_id = ?
                """,
                (next_stock, now, movement.item_id),
            )
            await db.execute(
                """
                INSERT INTO asset_movements (
                    movement_id, item_id, movement_type, quantity, actor_id,
                    actor_name, note, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    movement_id,
                    movement.item_id,
                    movement_type,
                    movement.quantity,
                    movement.actor_id,
                    movement.actor_name,
                    movement.note,
                    self._dumps(movement.metadata),
                    created_at,
                ),
            )
            await db.commit()

        updated = await self.get_asset_item(movement.item_id)
        if updated is None:
            raise LookupError(f"asset item {movement.item_id!r} not found after update")
        return updated

    async def list_asset_movements(
        self,
        *,
        item_id: str = "",
        limit: int = 100,
    ) -> list[AssetMovement]:
        """List recent asset movement rows.

        Args:
            item_id: Optional item ID filter.
            limit: Maximum rows to return.

        Returns:
            Recent movement rows.
        """

        await self.initialize()
        params: list[Any] = []
        query = "SELECT * FROM asset_movements"
        if item_id:
            query += " WHERE item_id = ?"
            params.append(item_id)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, int(limit)))
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(query, params)
            rows = await cursor.fetchall()
        return [self._asset_movement_from_row(row) for row in rows]

    async def upsert_finance_record(
        self,
        record: FinanceApprovalRecord,
    ) -> FinanceApprovalRecord:
        """Insert or update a finance approval record.

        Args:
            record: Finance approval record.

        Returns:
            Persisted record with updated_at populated.
        """

        await self.initialize()
        next_record = FinanceApprovalRecord(
            approval_instance_code=record.approval_instance_code,
            approval_type=record.approval_type,
            amount=float(record.amount or 0),
            department=record.department,
            applicant_id=record.applicant_id,
            applicant_name=record.applicant_name,
            status=record.status,
            attachment_status=record.attachment_status,
            missing_attachments=list(record.missing_attachments),
            approved_at=record.approved_at,
            source_record_id=record.source_record_id,
            updated_at=record.updated_at or self._utcnow(),
            metadata=dict(record.metadata),
        )
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO finance_approval_records (
                    approval_instance_code, approval_type, amount, department,
                    applicant_id, applicant_name, status, attachment_status,
                    missing_attachments_json, approved_at, source_record_id,
                    metadata_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(approval_instance_code) DO UPDATE SET
                    approval_type = excluded.approval_type,
                    amount = excluded.amount,
                    department = excluded.department,
                    applicant_id = excluded.applicant_id,
                    applicant_name = excluded.applicant_name,
                    status = excluded.status,
                    attachment_status = excluded.attachment_status,
                    missing_attachments_json = excluded.missing_attachments_json,
                    approved_at = excluded.approved_at,
                    source_record_id = excluded.source_record_id,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (
                    next_record.approval_instance_code,
                    next_record.approval_type,
                    next_record.amount,
                    next_record.department,
                    next_record.applicant_id,
                    next_record.applicant_name,
                    next_record.status,
                    next_record.attachment_status,
                    self._dumps(next_record.missing_attachments),
                    next_record.approved_at,
                    next_record.source_record_id,
                    self._dumps(next_record.metadata),
                    next_record.updated_at,
                ),
            )
            await db.commit()
        return next_record

    async def get_finance_record(
        self,
        approval_instance_code: str,
    ) -> FinanceApprovalRecord | None:
        """Read one finance approval record.

        Args:
            approval_instance_code: Feishu approval instance code or ID.

        Returns:
            FinanceApprovalRecord or None.
        """

        await self.initialize()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM finance_approval_records
                WHERE approval_instance_code = ?
                """,
                (approval_instance_code,),
            )
            row = await cursor.fetchone()
        return self._finance_record_from_row(row) if row else None

    async def list_finance_records(
        self,
        *,
        status: str = "",
        missing_attachments_only: bool = False,
        limit: int = 100,
    ) -> list[FinanceApprovalRecord]:
        """List finance approval records.

        Args:
            status: Optional approval status filter.
            missing_attachments_only: Whether to return only missing attachment rows.
            limit: Maximum rows to return.

        Returns:
            Finance approval records ordered by update time.
        """

        await self.initialize()
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if missing_attachments_only:
            clauses.append("attachment_status = 'missing'")
        query = "SELECT * FROM finance_approval_records"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(max(1, int(limit)))

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(query, params)
            rows = await cursor.fetchall()
        return [self._finance_record_from_row(row) for row in rows]

    async def upsert_onboarding_task(
        self,
        task: HrOnboardingTask,
    ) -> HrOnboardingTask:
        """Insert or update an onboarding task.

        Args:
            task: Onboarding task to persist.

        Returns:
            Persisted task with updated_at populated.
        """

        await self.initialize()
        next_task = HrOnboardingTask(
            task_id=task.task_id,
            employee_id=task.employee_id,
            employee_name=task.employee_name,
            department=task.department,
            owner_id=task.owner_id,
            owner_name=task.owner_name,
            task_name=task.task_name,
            status=task.status,
            due_at=task.due_at,
            completed_at=task.completed_at,
            updated_at=task.updated_at or self._utcnow(),
            metadata=dict(task.metadata),
        )
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO hr_onboarding_tasks (
                    task_id, employee_id, employee_name, department, owner_id,
                    owner_name, task_name, status, due_at, completed_at,
                    metadata_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    employee_id = excluded.employee_id,
                    employee_name = excluded.employee_name,
                    department = excluded.department,
                    owner_id = excluded.owner_id,
                    owner_name = excluded.owner_name,
                    task_name = excluded.task_name,
                    status = excluded.status,
                    due_at = excluded.due_at,
                    completed_at = excluded.completed_at,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (
                    next_task.task_id,
                    next_task.employee_id,
                    next_task.employee_name,
                    next_task.department,
                    next_task.owner_id,
                    next_task.owner_name,
                    next_task.task_name,
                    next_task.status,
                    next_task.due_at,
                    next_task.completed_at,
                    self._dumps(next_task.metadata),
                    next_task.updated_at,
                ),
            )
            await db.commit()
        return next_task

    async def list_onboarding_tasks(
        self,
        *,
        status: str = "",
        employee_id: str = "",
        open_only: bool = False,
        limit: int = 100,
    ) -> list[HrOnboardingTask]:
        """List HR onboarding tasks.

        Args:
            status: Optional exact status filter.
            employee_id: Optional employee filter.
            open_only: Whether to return only pending/in-progress/overdue tasks.
            limit: Maximum rows to return.

        Returns:
            Onboarding tasks ordered by due time.
        """

        await self.initialize()
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if employee_id:
            clauses.append("employee_id = ?")
            params.append(employee_id)
        if open_only:
            placeholders = ",".join("?" for _ in OPEN_ONBOARDING_STATUSES)
            clauses.append(f"status IN ({placeholders})")
            params.extend(sorted(OPEN_ONBOARDING_STATUSES))
        query = "SELECT * FROM hr_onboarding_tasks"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY due_at ASC, updated_at DESC LIMIT ?"
        params.append(max(1, int(limit)))

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(query, params)
            rows = await cursor.fetchall()
        return [self._onboarding_task_from_row(row) for row in rows]

    async def record_notification(self, log: NotificationLog) -> NotificationLog:
        """Persist a business notification audit row.

        Args:
            log: Notification log row.

        Returns:
            Persisted log with notification_id and created_at populated.
        """

        await self.initialize()
        next_log = NotificationLog(
            notification_id=log.notification_id or uuid.uuid4().hex,
            target_type=log.target_type,
            target_id=log.target_id,
            notification_type=log.notification_type,
            send_status=log.send_status,
            fail_reason=log.fail_reason,
            created_at=log.created_at or self._utcnow(),
            payload=dict(log.payload),
        )
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO business_notification_logs (
                    notification_id, target_type, target_id, notification_type,
                    send_status, fail_reason, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    next_log.notification_id,
                    next_log.target_type,
                    next_log.target_id,
                    next_log.notification_type,
                    next_log.send_status,
                    next_log.fail_reason,
                    self._dumps(next_log.payload),
                    next_log.created_at,
                ),
            )
            await db.commit()
        return next_log

    async def list_notification_logs(
        self, *, limit: int = 100
    ) -> list[NotificationLog]:
        """List recent notification audit rows.

        Args:
            limit: Maximum rows to return.

        Returns:
            Notification logs ordered by created_at descending.
        """

        await self.initialize()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM business_notification_logs
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            )
            rows = await cursor.fetchall()
        return [self._notification_log_from_row(row) for row in rows]

    async def snapshot(self) -> dict[str, Any]:
        """Build a dashboard-friendly business snapshot.

        Returns:
            Counts and short exception lists for admin, finance, HR, and health.
        """

        await self.initialize()
        low_stock = await self.list_asset_items(low_stock_only=True, limit=20)
        finance_missing = await self.list_finance_records(
            missing_attachments_only=True,
            limit=20,
        )
        pending_onboarding = await self.list_onboarding_tasks(open_only=True, limit=20)
        notifications = await self.list_notification_logs(limit=20)
        counts = await self._count_tables()
        return {
            "counts": counts,
            "low_stock_items": [self._asset_item_to_dict(item) for item in low_stock],
            "finance_missing_attachments": [
                self._finance_record_to_dict(record) for record in finance_missing
            ],
            "pending_onboarding_tasks": [
                self._onboarding_task_to_dict(task) for task in pending_onboarding
            ],
            "recent_notifications": [
                self._notification_log_to_dict(log) for log in notifications
            ],
        }

    async def _count_tables(self) -> dict[str, int]:
        """Count the dashboard table groups.

        Returns:
            Count values keyed by business area.
        """

        async with aiosqlite.connect(self.db_path) as db:
            counts: dict[str, int] = {}
            for key, table in (
                ("asset_items", "asset_items"),
                ("asset_movements", "asset_movements"),
                ("finance_approval_records", "finance_approval_records"),
                ("hr_onboarding_tasks", "hr_onboarding_tasks"),
                ("business_notification_logs", "business_notification_logs"),
            ):
                cursor = await db.execute(f"SELECT COUNT(*) FROM {table}")
                row = await cursor.fetchone()
                counts[key] = int(row[0] if row else 0)
            return counts

    @staticmethod
    def _utcnow() -> str:
        """Return a UTC ISO timestamp.

        Returns:
            UTC ISO timestamp string.
        """

        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _dumps(value: Any) -> str:
        """Serialize JSON with stable Chinese-friendly output.

        Args:
            value: JSON-serializable value.

        Returns:
            JSON string.
        """

        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _loads(raw: str, default: Any) -> Any:
        """Parse JSON and fall back on invalid rows.

        Args:
            raw: Raw JSON string.
            default: Value returned when parsing fails.

        Returns:
            Parsed JSON or default.
        """

        try:
            return json.loads(raw or "")
        except (TypeError, json.JSONDecodeError):
            return default

    def _asset_item_from_row(self, row: aiosqlite.Row) -> AssetItem:
        """Convert a SQLite row into an AssetItem.

        Args:
            row: SQLite row.

        Returns:
            AssetItem instance.
        """

        return AssetItem(
            item_id=str(row["item_id"]),
            name=str(row["name"]),
            category=str(row["category"]),
            stock=int(row["stock"]),
            warning_threshold=int(row["warning_threshold"]),
            custodian=str(row["custodian"]),
            status=str(row["status"]),
            updated_at=str(row["updated_at"]),
            metadata=dict(self._loads(row["metadata_json"], {})),
        )

    def _asset_movement_from_row(self, row: aiosqlite.Row) -> AssetMovement:
        """Convert a SQLite row into an AssetMovement.

        Args:
            row: SQLite row.

        Returns:
            AssetMovement instance.
        """

        return AssetMovement(
            movement_id=str(row["movement_id"]),
            item_id=str(row["item_id"]),
            movement_type=str(row["movement_type"]),
            quantity=int(row["quantity"]),
            actor_id=str(row["actor_id"]),
            actor_name=str(row["actor_name"]),
            note=str(row["note"]),
            created_at=str(row["created_at"]),
            metadata=dict(self._loads(row["metadata_json"], {})),
        )

    def _finance_record_from_row(self, row: aiosqlite.Row) -> FinanceApprovalRecord:
        """Convert a SQLite row into a FinanceApprovalRecord.

        Args:
            row: SQLite row.

        Returns:
            FinanceApprovalRecord instance.
        """

        missing = self._loads(row["missing_attachments_json"], [])
        return FinanceApprovalRecord(
            approval_instance_code=str(row["approval_instance_code"]),
            approval_type=str(row["approval_type"]),
            amount=float(row["amount"]),
            department=str(row["department"]),
            applicant_id=str(row["applicant_id"]),
            applicant_name=str(row["applicant_name"]),
            status=str(row["status"]),
            attachment_status=str(row["attachment_status"]),
            missing_attachments=list(missing if isinstance(missing, list) else []),
            approved_at=str(row["approved_at"]),
            source_record_id=str(row["source_record_id"]),
            updated_at=str(row["updated_at"]),
            metadata=dict(self._loads(row["metadata_json"], {})),
        )

    def _onboarding_task_from_row(self, row: aiosqlite.Row) -> HrOnboardingTask:
        """Convert a SQLite row into a HrOnboardingTask.

        Args:
            row: SQLite row.

        Returns:
            HrOnboardingTask instance.
        """

        return HrOnboardingTask(
            task_id=str(row["task_id"]),
            employee_id=str(row["employee_id"]),
            employee_name=str(row["employee_name"]),
            department=str(row["department"]),
            owner_id=str(row["owner_id"]),
            owner_name=str(row["owner_name"]),
            task_name=str(row["task_name"]),
            status=str(row["status"]),
            due_at=str(row["due_at"]),
            completed_at=str(row["completed_at"]),
            updated_at=str(row["updated_at"]),
            metadata=dict(self._loads(row["metadata_json"], {})),
        )

    def _notification_log_from_row(self, row: aiosqlite.Row) -> NotificationLog:
        """Convert a SQLite row into a NotificationLog.

        Args:
            row: SQLite row.

        Returns:
            NotificationLog instance.
        """

        return NotificationLog(
            notification_id=str(row["notification_id"]),
            target_type=str(row["target_type"]),
            target_id=str(row["target_id"]),
            notification_type=str(row["notification_type"]),
            send_status=str(row["send_status"]),
            fail_reason=str(row["fail_reason"]),
            created_at=str(row["created_at"]),
            payload=dict(self._loads(row["payload_json"], {})),
        )

    @staticmethod
    def _asset_item_to_dict(item: AssetItem) -> dict[str, Any]:
        """Convert an asset item to a plain dict.

        Args:
            item: Asset item.

        Returns:
            JSON-friendly dict.
        """

        return {
            "item_id": item.item_id,
            "name": item.name,
            "category": item.category,
            "stock": item.stock,
            "warning_threshold": item.warning_threshold,
            "custodian": item.custodian,
            "status": item.status,
            "low_stock": item.low_stock,
            "updated_at": item.updated_at,
            "metadata": item.metadata,
        }

    @staticmethod
    def _finance_record_to_dict(record: FinanceApprovalRecord) -> dict[str, Any]:
        """Convert a finance approval record to a plain dict.

        Args:
            record: Finance approval record.

        Returns:
            JSON-friendly dict.
        """

        return {
            "approval_instance_code": record.approval_instance_code,
            "approval_type": record.approval_type,
            "amount": record.amount,
            "department": record.department,
            "applicant_id": record.applicant_id,
            "applicant_name": record.applicant_name,
            "status": record.status,
            "attachment_status": record.attachment_status,
            "missing_attachments": record.missing_attachments,
            "approved_at": record.approved_at,
            "source_record_id": record.source_record_id,
            "updated_at": record.updated_at,
            "metadata": record.metadata,
        }

    @staticmethod
    def _onboarding_task_to_dict(task: HrOnboardingTask) -> dict[str, Any]:
        """Convert an onboarding task to a plain dict.

        Args:
            task: Onboarding task.

        Returns:
            JSON-friendly dict.
        """

        return {
            "task_id": task.task_id,
            "employee_id": task.employee_id,
            "employee_name": task.employee_name,
            "department": task.department,
            "owner_id": task.owner_id,
            "owner_name": task.owner_name,
            "task_name": task.task_name,
            "status": task.status,
            "due_at": task.due_at,
            "completed_at": task.completed_at,
            "updated_at": task.updated_at,
            "metadata": task.metadata,
        }

    @staticmethod
    def _notification_log_to_dict(log: NotificationLog) -> dict[str, Any]:
        """Convert a notification log to a plain dict.

        Args:
            log: Notification log.

        Returns:
            JSON-friendly dict.
        """

        return {
            "notification_id": log.notification_id,
            "target_type": log.target_type,
            "target_id": log.target_id,
            "notification_type": log.notification_type,
            "send_status": log.send_status,
            "fail_reason": log.fail_reason,
            "created_at": log.created_at,
            "payload": log.payload,
        }
