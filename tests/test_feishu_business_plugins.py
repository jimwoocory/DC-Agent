from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dc_engines"))

from dc_engines.feishu_business_mvp import (  # noqa: E402
    AssetItem,
    FinanceApprovalRecord,
    HrOnboardingTask,
)

from data.plugins.admin_asset_plugin.main import AdminAssetPlugin  # noqa: E402
from data.plugins.feishu_business_report_plugin.main import (  # noqa: E402
    FeishuBusinessReportPlugin,
)
from data.plugins.finance_workflow_plugin.main import (
    FinanceWorkflowPlugin,  # noqa: E402
)
from data.plugins.hr_admin_workflow_plugin.main import (
    HrAdminWorkflowPlugin,  # noqa: E402
)


class _FakeContext:
    def __init__(self) -> None:
        self.apis: list[tuple[str, object, list[str], str]] = []

    def register_web_api(self, route, view_handler, methods, desc):
        self.apis.append((route, view_handler, methods, desc))


@pytest.mark.asyncio
async def test_business_plugins_register_expected_web_apis(tmp_path) -> None:
    ctx = _FakeContext()
    config = {"enabled": False, "db_path": str(tmp_path / "business.db")}
    plugins = [
        AdminAssetPlugin(ctx, config),
        FinanceWorkflowPlugin(ctx, config),
        HrAdminWorkflowPlugin(ctx, config),
        FeishuBusinessReportPlugin(ctx, config),
    ]

    for plugin in plugins:
        await plugin.initialize()

    routes = {route for route, _handler, _methods, _desc in ctx.apis}

    assert "/feishu_business/admin/assets" in routes
    assert "/feishu_business/admin/assets/movement" in routes
    assert "/feishu_business/finance/records" in routes
    assert "/feishu_business/finance/missing-attachments" in routes
    assert "/feishu_business/finance/sync" in routes
    assert "/feishu_business/hr/onboarding" in routes
    assert "/feishu_business/summary" in routes
    assert "/feishu_business/health" in routes
    assert "/feishu_business/run" in routes


@pytest.mark.asyncio
async def test_business_plugin_apis_return_disabled_health_without_feishu(
    tmp_path,
) -> None:
    ctx = _FakeContext()
    config = {"enabled": False, "db_path": str(tmp_path / "business.db")}
    admin = AdminAssetPlugin(ctx, config)
    finance = FinanceWorkflowPlugin(ctx, config)
    hr = HrAdminWorkflowPlugin(ctx, config)
    report = FeishuBusinessReportPlugin(ctx, config)

    for plugin in (admin, finance, hr, report):
        await plugin.initialize()

    await admin.store.upsert_asset_item(
        AssetItem(item_id="paper", name="A4 纸", stock=1, warning_threshold=3)
    )
    await finance.store.upsert_finance_record(
        FinanceApprovalRecord(
            approval_instance_code="fin_1",
            approval_type="reimbursement",
            attachment_status="missing",
            missing_attachments=["发票"],
        )
    )
    await hr.store.upsert_onboarding_task(
        HrOnboardingTask(
            task_id="hr_1",
            employee_id="u1",
            employee_name="赵六",
            task_name="账号开通",
        )
    )

    admin_payload = await admin._api_assets()
    finance_payload = await finance._api_missing_attachments()
    summary_payload = await report._api_summary()
    health_payload = await report._api_health()

    assert admin_payload["data"]["enabled"] is False
    assert admin_payload["data"]["low_stock"][0]["item_id"] == "paper"
    assert finance_payload["data"]["records"][0]["missing_attachments"] == ["发票"]
    assert summary_payload["data"]["counts"]["asset_items"] == 1
    assert (
        summary_payload["data"]["pending_onboarding_tasks"][0]["employee_name"]
        == "赵六"
    )
    assert health_payload["data"]["enabled"] is False
    assert "hub_stats" in health_payload["data"]


class _FakeEvent:
    def __init__(self, text: str) -> None:
        self.message_str = text
        self.result = None

    def set_result(self, result) -> None:
        self.result = result


def test_admin_asset_question_matcher_detects_office_supply_questions() -> None:
    assert AdminAssetPlugin._should_handle_asset_question("怎么领办公用品？") is True
    assert AdminAssetPlugin._should_handle_asset_question("办公用品库存还有吗") is True
    assert AdminAssetPlugin._should_handle_asset_question("今天吃什么") is False


@pytest.mark.asyncio
async def test_admin_asset_natural_language_question_replies_with_inventory(
    tmp_path,
) -> None:
    ctx = _FakeContext()
    plugin = AdminAssetPlugin(
        ctx,
        {"enabled": True, "db_path": str(tmp_path / "business.db")},
    )
    await plugin.initialize()
    await plugin.store.upsert_asset_item(
        AssetItem(item_id="paper", name="A4 纸", stock=5, warning_threshold=2)
    )
    event = _FakeEvent("A4 纸怎么领办公用品")

    await plugin.on_asset_question(event)

    assert event.result is not None
