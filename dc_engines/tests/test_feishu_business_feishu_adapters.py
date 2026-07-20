from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from dc_engines.feishu_business_mvp import (
    ApprovalSyncer,
    BitableLocation,
    BusinessMvpStore,
    BusinessNotifier,
    FeishuBitableClient,
    FinanceApprovalRecord,
)


class _Resp:
    def __init__(self, *, data=None, ok: bool = True) -> None:
        self.data = data
        self.code = "bad"
        self.msg = "failed"
        self._ok = ok

    def success(self) -> bool:
        return self._ok


class _RecordApi:
    async def alist(self, _req):
        return _Resp(
            data=SimpleNamespace(
                items=[
                    SimpleNamespace(record_id="rec_1", fields={"名称": "中性笔"}),
                ],
                has_more=False,
            )
        )

    async def asearch(self, _req):
        return _Resp(
            data=SimpleNamespace(
                items=[
                    SimpleNamespace(record_id="rec_2", fields={"名称": "订书机"}),
                ],
                has_more=False,
            )
        )

    async def acreate(self, _req):
        return _Resp(
            data=SimpleNamespace(
                record=SimpleNamespace(record_id="rec_new", fields={"名称": "纸巾"})
            )
        )

    async def aupdate(self, _req):
        return _Resp(
            data=SimpleNamespace(
                record=SimpleNamespace(record_id="rec_new", fields={"库存": 10})
            )
        )


class _ApprovalApi:
    async def alist(self, _req):
        return _Resp(
            data=SimpleNamespace(instance_code_list=["inst_1"], has_more=False)
        )

    async def aget(self, _req):
        return _Resp(
            data=SimpleNamespace(
                instance={
                    "status": "approved",
                    "form": json.dumps(
                        [
                            {"name": "申请人", "value": "王五"},
                            {"name": "金额", "value": "88.60"},
                            {"name": "发票", "value": [{"file_token": "file_1"}]},
                            {"name": "合同", "value": []},
                        ],
                        ensure_ascii=False,
                    ),
                }
            )
        )


class _MessageApi:
    async def acreate(self, _req):
        return _Resp(data=SimpleNamespace(message_id="msg_1"))


def _fake_bitable_client():
    return SimpleNamespace(
        bitable=SimpleNamespace(
            v1=SimpleNamespace(app_table_record=_RecordApi()),
        )
    )


def _fake_approval_client():
    return SimpleNamespace(
        approval=SimpleNamespace(v4=SimpleNamespace(instance=_ApprovalApi())),
    )


def _fake_message_client():
    return SimpleNamespace(
        im=SimpleNamespace(v1=SimpleNamespace(message=_MessageApi()))
    )


@pytest.mark.asyncio
async def test_bitable_disabled_branch_does_not_call_feishu() -> None:
    methods: list[str] = []

    async def fake_call(method, coro):
        methods.append(method)
        return await coro

    client = FeishuBitableClient(client=None, enabled=False, call_fn=fake_call)
    location = BitableLocation(app_token="app", table_id="tbl")

    assert await client.list_records(location) == []
    assert await client.create_record(location, {"名称": "纸巾"}) is None
    assert methods == []


@pytest.mark.asyncio
async def test_bitable_calls_route_through_hub_call() -> None:
    methods: list[str] = []

    async def fake_call(method, coro):
        methods.append(method)
        return await coro

    client = FeishuBitableClient(
        client=_fake_bitable_client(),
        enabled=True,
        call_fn=fake_call,
    )
    location = BitableLocation(app_token="app", table_id="tbl")

    listed = await client.list_records(location)
    searched = await client.search_records(location, filter_body={"conditions": []})
    created = await client.create_record(location, {"名称": "纸巾"})
    updated = await client.update_record(location, "rec_new", {"库存": 10})

    assert listed[0].record_id == "rec_1"
    assert searched[0].record_id == "rec_2"
    assert created and created.record_id == "rec_new"
    assert updated and updated.fields["库存"] == 10
    assert methods == [
        "bitable.app_table_record.list",
        "bitable.app_table_record.search",
        "bitable.app_table_record.create",
        "bitable.app_table_record.update",
    ]


@pytest.mark.asyncio
async def test_approval_sync_uses_hub_call_and_checks_attachments(tmp_path) -> None:
    methods: list[str] = []
    store = BusinessMvpStore(tmp_path / "business.db")

    async def fake_call(method, coro):
        methods.append(method)
        return await coro

    syncer = ApprovalSyncer(
        store=store,
        client=_fake_approval_client(),
        enabled=True,
        call_fn=fake_call,
        required_attachments={"reimbursement": ["发票", "合同"]},
    )

    records = await syncer.sync_finance_approvals(
        "approval_code",
        "reimbursement",
        start_time=1,
        end_time=2,
    )
    stored = await store.get_finance_record("inst_1")

    assert methods == ["approval.instance.list", "approval.instance.get"]
    assert records[0].amount == 88.6
    assert records[0].attachment_status == "missing"
    assert records[0].missing_attachments == ["合同"]
    assert stored is not None
    assert stored.applicant_name == "王五"


def test_approval_record_supports_generic_attachment_and_summary_amount() -> None:
    syncer = ApprovalSyncer(
        enabled=False,
        required_attachments={"reimbursement": ["发票"]},
    )

    record = syncer.finance_record_from_detail(
        "inst_2",
        "reimbursement",
        {
            "status": "PENDING",
            "form": json.dumps(
                [
                    {"name": "报销事由", "type": "textarea", "value": "广州"},
                    {"name": "费用汇总", "type": "formula", "value": 300},
                    {
                        "name": "附件",
                        "type": "attachmentV2",
                        "value": ["https://example.test/invoice.pdf"],
                    },
                ],
                ensure_ascii=False,
            ),
        },
    )

    assert record.amount == 300.0
    assert record.attachment_status == "complete"
    assert record.missing_attachments == []


@pytest.mark.asyncio
async def test_invoice_review_keeps_reimbursement_and_invoice_amounts_separate(
    tmp_path,
) -> None:
    store = BusinessMvpStore(tmp_path / "business.db")
    await store.upsert_finance_record(
        FinanceApprovalRecord(
            approval_instance_code="prior_1",
            approval_type="reimbursement",
            metadata={
                "invoice_review": {
                    "invoices": [{"invoice_number": "12345678901234567890"}]
                }
            },
        )
    )

    async def fake_fetch(url: str) -> bytes:
        assert url == "https://example.test/invoice.pdf"
        return b"invoice-pdf"

    syncer = ApprovalSyncer(
        store=store,
        enabled=False,
        attachment_fetcher=fake_fetch,
        invoice_text_extractor=lambda _content: (
            "电子发票（普通发票） 发票号码： 开票日期： "
            "12345678901234567890 价税合计（小写） ￥278.10"
        ),
    )
    record = syncer.finance_record_from_detail(
        "inst_3",
        "reimbursement",
        {
            "form": json.dumps(
                [
                    {"name": "费用汇总", "value": 300},
                    {
                        "name": "附件",
                        "value": ["https://example.test/invoice.pdf"],
                    },
                ],
                ensure_ascii=False,
            )
        },
    )

    review = await syncer._review_invoice_record(record)

    assert review["reimbursement_amount"] == 300.0
    assert review["invoice_total"] == 278.1
    assert review["status"] == "manual_review"
    assert set(review["reasons"]) == {
        "duplicate_invoice",
        "invoice_total_differs_from_reimbursement_total",
    }
    assert review["duplicate_instance_codes"] == ["prior_1"]


@pytest.mark.asyncio
async def test_notifier_disabled_and_enabled_paths_log_results(tmp_path) -> None:
    store = BusinessMvpStore(tmp_path / "business.db")
    methods: list[str] = []

    async def fake_call(method, coro):
        methods.append(method)
        return await coro

    disabled = BusinessNotifier(
        store=store, client=None, enabled=False, call_fn=fake_call
    )
    disabled_log = await disabled.send_text(target_id="ou_1", text="缺发票")

    enabled = BusinessNotifier(
        store=store,
        client=_fake_message_client(),
        enabled=True,
        call_fn=fake_call,
    )
    sent_log = await enabled.send_card(
        target_id="oc_1", receive_id_type="chat_id", card={}
    )

    logs = await store.list_notification_logs()

    assert disabled_log.send_status == "disabled"
    assert sent_log.send_status == "sent"
    assert methods == ["im.message.create"]
    assert {log.send_status for log in logs} == {"disabled", "sent"}
