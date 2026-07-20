from __future__ import annotations

import sqlite3

import pytest
from dc_engines.feishu_business_mvp import (
    AssetItem,
    AssetMovement,
    BitableLocation,
    BitableRecord,
    BusinessMvpStore,
    BusinessWorkflowRunner,
    FinanceApprovalRecord,
    HrOnboardingTask,
    NotificationLog,
    assess_attachment_status,
    business_mvp_config_from_dict,
    find_forbidden_secret_keys,
)
from dc_engines.feishu_business_mvp.contracts import BusinessMvpConfig


@pytest.mark.asyncio
async def test_business_store_creates_required_tables(tmp_path) -> None:
    store = BusinessMvpStore(tmp_path / "business.db")
    await store.initialize()

    with sqlite3.connect(store.db_path) as db:
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()

    assert {
        "asset_items",
        "asset_movements",
        "finance_approval_records",
        "hr_onboarding_tasks",
        "business_notification_logs",
    }.issubset({row[0] for row in rows})


@pytest.mark.asyncio
async def test_asset_stock_movements_and_low_stock(tmp_path) -> None:
    store = BusinessMvpStore(tmp_path / "business.db")
    await store.upsert_asset_item(
        AssetItem(
            item_id="pen",
            name="中性笔",
            category="文具",
            stock=5,
            warning_threshold=3,
        )
    )

    claimed = await store.apply_asset_movement(
        AssetMovement(
            movement_id="m1",
            item_id="pen",
            movement_type="claim",
            quantity=2,
        )
    )
    returned = await store.apply_asset_movement(
        AssetMovement(
            movement_id="m2",
            item_id="pen",
            movement_type="return",
            quantity=1,
        )
    )

    assert claimed.stock == 3
    assert returned.stock == 4
    assert await store.list_asset_items(low_stock_only=True) == []

    adjusted = await store.apply_asset_movement(
        AssetMovement(
            movement_id="m3",
            item_id="pen",
            movement_type="inventory_adjust",
            quantity=0,
        )
    )
    assert adjusted.stock == 0
    assert adjusted.low_stock is True
    assert [
        item.item_id for item in await store.list_asset_items(low_stock_only=True)
    ] == ["pen"]

    with pytest.raises(ValueError):
        await store.apply_asset_movement(
            AssetMovement(
                movement_id="m4",
                item_id="pen",
                movement_type="claim",
                quantity=9,
            )
        )


def test_finance_attachment_status_detects_missing_required_files() -> None:
    status, missing = assess_attachment_status(
        {
            "发票附件": [{"file_token": "file_invoice"}],
            "合同附件": [],
            "付款截图": "付款截图.png",
        },
        ["发票", "合同", "付款截图"],
    )

    assert status == "missing"
    assert missing == ["合同"]


@pytest.mark.asyncio
async def test_finance_hr_notification_snapshot(tmp_path) -> None:
    store = BusinessMvpStore(tmp_path / "business.db")
    await store.upsert_finance_record(
        FinanceApprovalRecord(
            approval_instance_code="approval_1",
            approval_type="reimbursement",
            amount=128.5,
            applicant_name="张三",
            attachment_status="missing",
            missing_attachments=["发票"],
        )
    )
    await store.upsert_onboarding_task(
        HrOnboardingTask(
            task_id="task_1",
            employee_id="u1",
            employee_name="李四",
            department="综合部",
            task_name="账号开通",
        )
    )
    await store.record_notification(
        NotificationLog(
            notification_id="",
            target_type="open_id",
            target_id="ou_1",
            notification_type="finance_missing_attachment",
            send_status="disabled",
        )
    )

    snapshot = await store.snapshot()

    assert snapshot["counts"]["finance_approval_records"] == 1
    assert snapshot["counts"]["hr_onboarding_tasks"] == 1
    assert snapshot["counts"]["business_notification_logs"] == 1
    assert (
        snapshot["finance_missing_attachments"][0]["approval_instance_code"]
        == "approval_1"
    )
    assert snapshot["pending_onboarding_tasks"][0]["employee_name"] == "李四"


def test_business_config_loader_parses_business_fields_without_secrets() -> None:
    config = business_mvp_config_from_dict(
        {
            "enabled": True,
            "db_path": "data/custom.db",
            "sync_window_hours": 24,
            "asset_table": {
                "app_token": "app_asset",
                "table_id": "tbl_asset",
                "app_secret": "must_not_be_kept",
            },
            "approval_codes": {"reimbursement": "approval_1"},
            "required_finance_attachments": {"reimbursement": ["发票", "合同"]},
            "notification_targets": {"management_chat_id": "oc_1"},
        }
    )

    assert config.db_path == "data/custom.db"
    assert config.sync_window_hours == 24
    assert config.asset_table.app_token == "app_asset"
    assert config.asset_table.table_id == "tbl_asset"
    assert not hasattr(config.asset_table, "app_secret")
    assert config.approval_codes == {"reimbursement": "approval_1"}
    assert config.notification_targets["management_chat_id"] == "oc_1"
    assert find_forbidden_secret_keys({"asset_table": {"app_token": "app"}}) == []
    assert find_forbidden_secret_keys({"app_secret": "secret"}) == ["app_secret"]


class _FakeBitable:
    enabled = True

    async def list_records(self, location: BitableLocation, *, limit: int = 500):
        if location.table_id == "asset_table":
            return [
                BitableRecord(
                    record_id="rec_asset",
                    fields={
                        "物品ID": "paper",
                        "物品名称": "A4 纸",
                        "类别": "文具",
                        "库存": 2,
                        "预警线": 3,
                        "保管人": "行政",
                    },
                )
            ]
        if location.table_id == "onboarding_table":
            return [
                BitableRecord(
                    record_id="rec_hr",
                    fields={
                        "新人": "赵六",
                        "部门": "综合部",
                        "负责人": "王主管",
                        "待办项": "账号开通,工位确认",
                    },
                )
            ]
        return []


class _FakeApprovalSyncer:
    enabled = True

    def __init__(self, store: BusinessMvpStore) -> None:
        self.store = store

    async def sync_finance_approvals(
        self,
        approval_code: str,
        approval_type: str,
        *,
        start_time: int,
        end_time: int,
    ):
        record = FinanceApprovalRecord(
            approval_instance_code=f"{approval_type}_1",
            approval_type=approval_type,
            applicant_id="ou_1",
            applicant_name="张三",
            attachment_status="missing",
            missing_attachments=["发票"],
            metadata={
                "approval_code": approval_code,
                "start_time": start_time,
                "end_time": end_time,
            },
        )
        await self.store.upsert_finance_record(record)
        return [record]


class _FakeNotifier:
    def __init__(self, store: BusinessMvpStore) -> None:
        self.store = store
        self.messages: list[dict] = []

    async def send_text(
        self,
        *,
        target_id: str,
        text: str,
        receive_id_type: str = "open_id",
        notification_type: str = "business_text",
    ):
        self.messages.append(
            {
                "target_id": target_id,
                "text": text,
                "receive_id_type": receive_id_type,
                "notification_type": notification_type,
            }
        )
        return await self.store.record_notification(
            NotificationLog(
                notification_id="",
                target_type=receive_id_type,
                target_id=target_id,
                notification_type=notification_type,
                send_status="sent" if target_id else "skipped",
                payload={"text": text},
            )
        )


@pytest.mark.asyncio
async def test_business_workflow_runner_syncs_and_notifies(tmp_path) -> None:
    store = BusinessMvpStore(tmp_path / "business.db")
    config = BusinessMvpConfig(
        db_path=store.db_path,
        asset_table=BitableLocation(app_token="app", table_id="asset_table"),
        onboarding_table=BitableLocation(app_token="app", table_id="onboarding_table"),
        approval_codes={"reimbursement": "approval_code_1"},
        notification_targets={
            "admin_chat_id": "oc_admin",
            "finance_chat_id": "oc_finance",
            "management_chat_id": "oc_management",
        },
        sync_window_hours=24,
    )
    notifier = _FakeNotifier(store)
    runner = BusinessWorkflowRunner(
        config=config,
        store=store,
        bitable=_FakeBitable(),
        approval_syncer=_FakeApprovalSyncer(store),
        notifier=notifier,
    )

    result = await runner.run_once(
        [
            "preflight",
            "sync-assets",
            "sync-onboarding",
            "sync-finance",
            "notify-exceptions",
            "weekly-report",
        ]
    )

    assert result["preflight"]["status"] == "ready"
    assert result["sync-assets"]["synced"] == 1
    assert result["sync-onboarding"]["synced"] == 2
    assert result["sync-finance"]["synced"] == 1
    assert result["notify-low-stock"]["count"] == 1
    assert result["notify-finance"]["records"] == 1
    assert result["weekly-report"]["send_status"] == "sent"
    assert result["snapshot"]["counts"]["asset_items"] == 1
    assert result["snapshot"]["counts"]["hr_onboarding_tasks"] == 2
    assert {message["receive_id_type"] for message in notifier.messages} == {
        "chat_id",
        "open_id",
    }


@pytest.mark.asyncio
async def test_finance_manual_review_notifies_once_per_same_conclusion(
    tmp_path,
) -> None:
    class _ManualReviewApprovalSyncer:
        enabled = True

        async def sync_finance_approvals(
            self,
            _approval_code: str,
            approval_type: str,
            *,
            start_time: int,
            end_time: int,
        ):
            return [
                FinanceApprovalRecord(
                    approval_instance_code="review_1",
                    approval_type=approval_type,
                    amount=300,
                    metadata={
                        "invoice_review": {
                            "status": "manual_review",
                            "invoice_total": 278.1,
                            "reasons": [
                                "invoice_total_differs_from_reimbursement_total"
                            ],
                        },
                        "start_time": start_time,
                        "end_time": end_time,
                    },
                )
            ]

    store = BusinessMvpStore(tmp_path / "business.db")
    notifier = _FakeNotifier(store)
    runner = BusinessWorkflowRunner(
        config=BusinessMvpConfig(
            db_path=store.db_path,
            approval_codes={"reimbursement": "approval_code_1"},
            notification_targets={"finance_open_id": "ou_finance"},
        ),
        store=store,
        bitable=_FakeBitable(),
        approval_syncer=_ManualReviewApprovalSyncer(),
        notifier=notifier,
    )

    first = await runner.sync_finance_approvals()
    second = await runner.sync_finance_approvals()

    assert first["review_notifications"] == 1
    assert second["review_notifications"] == 0
    assert len(notifier.messages) == 1
    assert notifier.messages[0]["target_id"] == "ou_finance"


@pytest.mark.asyncio
async def test_business_workflow_preflight_blocks_for_secret_keys(tmp_path) -> None:
    store = BusinessMvpStore(tmp_path / "business.db")
    config = BusinessMvpConfig(db_path=store.db_path)
    runner = BusinessWorkflowRunner(
        config=config,
        store=store,
        bitable=_FakeBitable(),
        approval_syncer=_FakeApprovalSyncer(store),
        notifier=_FakeNotifier(store),
    )

    result = await runner.run_once(["preflight"], raw_config={"app_secret": "secret"})

    assert result["preflight"]["status"] == "blocked"
    assert result["preflight"]["issues"][0]["code"] == "forbidden_secret_key"
