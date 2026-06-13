from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
from dc_engines.feishu_reader import (
    DocContent,
    FeishuClient,
    FeishuCredentials,
    QueryHit,
    TableRecord,
    Whitelist,
    WhitelistDocument,
    WhitelistTable,
    query_resources_v1,
)
from dc_engines.feishu_reader.client import FeishuClientError


class FakeFeishuClient:
    def __init__(
        self,
        *,
        enabled: bool = True,
        documents: dict[str, DocContent | Exception | None] | None = None,
        tables: dict[tuple[str, str], list[TableRecord] | Exception | None]
        | None = None,
    ) -> None:
        self.enabled = enabled
        self.documents = documents or {}
        self.tables = tables or {}
        self.document_calls: list[str] = []
        self.table_calls: list[tuple[str, str]] = []
        self.table_record_limits: list[int] = []

    async def read_document(self, doc_token: str) -> DocContent | None:
        self.document_calls.append(doc_token)
        result = self.documents.get(doc_token)
        if isinstance(result, Exception):
            raise result
        return result

    async def read_table_records(
        self,
        app_token: str,
        table_id: str,
        *,
        limit: int = 500,
    ) -> list[TableRecord]:
        self.table_calls.append((app_token, table_id))
        self.table_record_limits.append(limit)
        result = self.tables.get((app_token, table_id), [])
        if isinstance(result, Exception):
            raise result
        return result


class SlowFeishuClient:
    enabled = True

    def __init__(self, *, delay_seconds: float) -> None:
        self.delay_seconds = delay_seconds
        self.document_calls: list[str] = []
        self.table_calls: list[tuple[str, str]] = []

    async def read_document(self, doc_token: str) -> DocContent | None:
        self.document_calls.append(doc_token)
        await asyncio.sleep(self.delay_seconds)
        return DocContent(
            doc_token=doc_token,
            title="Slow document",
            plain_text="Alpha content after slow read",
            block_count=1,
        )

    async def read_table_records(
        self,
        app_token: str,
        table_id: str,
        *,
        limit: int = 500,
    ) -> list[TableRecord]:
        self.table_calls.append((app_token, table_id))
        await asyncio.sleep(self.delay_seconds)
        return [
            TableRecord(
                record_id="rec_slow",
                fields={"status": "Alpha content after slow read"},
            )
        ]


class _FakeLarkResponse:
    def __init__(
        self,
        *,
        ok: bool,
        data=None,
        code: str = "api_error",
        msg: str = "failed",
    ) -> None:
        self.data = data
        self.code = code
        self.msg = msg
        self._ok = ok

    def success(self) -> bool:
        return self._ok


def _make_production_client(fake_lark_client) -> FeishuClient:
    client = FeishuClient.__new__(FeishuClient)
    client.credentials = FeishuCredentials(
        app_id="cli_fake",
        app_secret="secret_fake",
        enable=True,
    )
    client._client = fake_lark_client
    return client


def _make_lark_client(*, document_api=None, block_api=None, record_api=None):
    return SimpleNamespace(
        docx=SimpleNamespace(
            v1=SimpleNamespace(
                document=document_api or SimpleNamespace(),
                document_block=block_api or SimpleNamespace(),
            )
        ),
        bitable=SimpleNamespace(
            v1=SimpleNamespace(
                app_table_record=record_api or SimpleNamespace(),
            )
        ),
    )


def test_query_hit_metadata_defaults_for_legacy_callers() -> None:
    hit = QueryHit(
        source_type="document",
        source_id="doxc_legacy",
        title="Legacy hit",
        domain="general",
    )

    assert hit.metadata == {}


@pytest.mark.asyncio
async def test_enabled_document_content_hit_has_content_provenance() -> None:
    whitelist = Whitelist(
        documents=[
            WhitelistDocument(
                doc_token="doxc_pricing",
                name="Pricing playbook",
                domain="sales",
                description="Commercial FAQ",
            )
        ]
    )
    client = FakeFeishuClient(
        documents={
            "doxc_pricing": DocContent(
                doc_token="doxc_pricing",
                title="Pricing playbook",
                plain_text="Renewal pricing requires finance approval.",
                block_count=2,
            )
        }
    )

    hits = await query_resources_v1("finance", whitelist=whitelist, client=client)

    assert len(hits) == 1
    hit = hits[0]
    assert hit.matched_field == "content"
    assert "finance" in hit.matched_snippet
    assert hit.metadata["retrieval_mode"] == "content"
    assert hit.metadata["credential_status"] == "enabled"
    assert hit.metadata["source_type"] == "document"


@pytest.mark.asyncio
async def test_enabled_table_content_hit_has_content_provenance() -> None:
    whitelist = Whitelist(
        tables=[
            WhitelistTable(
                app_token="base_sales",
                table_id="tbl_accounts",
                name="Account tracker",
                domain="sales",
                description="Customer records",
            )
        ]
    )
    client = FakeFeishuClient(
        tables={
            ("base_sales", "tbl_accounts"): [
                TableRecord(
                    record_id="rec_1",
                    fields={
                        "customer": "Acme",
                        "status": "Finance review complete",
                    },
                )
            ]
        }
    )

    hits = await query_resources_v1("Finance", whitelist=whitelist, client=client)

    assert len(hits) == 1
    hit = hits[0]
    assert hit.source_type == "table"
    assert hit.matched_field == "content"
    assert "Finance" in hit.matched_snippet
    assert hit.metadata["retrieval_mode"] == "content"
    assert hit.metadata["credential_status"] == "enabled"
    assert hit.metadata["source_type"] == "table"


@pytest.mark.asyncio
async def test_disabled_client_returns_metadata_only_hit_without_content_search() -> (
    None
):
    whitelist = Whitelist(
        documents=[
            WhitelistDocument(
                doc_token="doxc_handbook",
                name="Employee handbook",
                domain="people",
                description="Benefits and policies",
            )
        ]
    )
    client = FakeFeishuClient(enabled=False)

    hits = await query_resources_v1("handbook", whitelist=whitelist, client=client)

    assert len(hits) == 1
    hit = hits[0]
    assert hit.matched_field == "meta"
    assert hit.matched_snippet == ""
    assert hit.metadata["retrieval_mode"] == "metadata_only"
    assert hit.metadata["credential_status"] == "disabled"
    assert client.document_calls == []


@pytest.mark.asyncio
async def test_source_exception_falls_back_to_matching_metadata_and_skips_misses() -> (
    None
):
    whitelist = Whitelist(
        documents=[
            WhitelistDocument(
                doc_token="doxc_alpha",
                name="Alpha incident runbook",
                domain="ops",
                description="Escalation steps",
            ),
            WhitelistDocument(
                doc_token="doxc_beta",
                name="Beta checklist",
                domain="ops",
                description="Release notes",
            ),
        ]
    )
    client = FakeFeishuClient(
        documents={
            "doxc_alpha": RuntimeError("doc API unavailable"),
            "doxc_beta": RuntimeError("doc API unavailable"),
        }
    )

    hits = await query_resources_v1("Alpha", whitelist=whitelist, client=client)

    assert [hit.source_id for hit in hits] == ["doxc_alpha"]
    hit = hits[0]
    assert hit.matched_field == "meta"
    assert hit.matched_snippet == ""
    assert hit.metadata["retrieval_mode"] == "metadata_only"
    assert hit.metadata["credential_status"] == "enabled"
    assert hit.metadata["content_status"] == "error"
    assert hit.metadata["error_type"] == "RuntimeError"


@pytest.mark.asyncio
async def test_slow_document_content_times_out_with_error_provenance() -> None:
    whitelist = Whitelist(
        documents=[
            WhitelistDocument(
                doc_token="doxc_alpha_slow",
                name="Alpha slow runbook",
                domain="ops",
                description="Escalation steps",
            )
        ]
    )
    client = SlowFeishuClient(delay_seconds=1.0)

    started = time.perf_counter()
    hits = await query_resources_v1(
        "Alpha",
        whitelist=whitelist,
        client=client,
        content_timeout_seconds=0.01,
    )
    elapsed = time.perf_counter() - started

    assert elapsed < 0.5
    assert client.document_calls == ["doxc_alpha_slow"]
    assert len(hits) == 1
    hit = hits[0]
    assert hit.matched_field == "meta"
    assert hit.matched_snippet == ""
    assert hit.metadata["retrieval_mode"] == "metadata_only"
    assert hit.metadata["content_status"] == "error"
    assert hit.metadata["error_type"] == "TimeoutError"


@pytest.mark.asyncio
async def test_slow_table_content_times_out_with_error_provenance() -> None:
    whitelist = Whitelist(
        tables=[
            WhitelistTable(
                app_token="base_alpha_slow",
                table_id="tbl_alpha_slow",
                name="Alpha slow tracker",
                domain="ops",
                description="Customer records",
            )
        ]
    )
    client = SlowFeishuClient(delay_seconds=1.0)

    started = time.perf_counter()
    hits = await query_resources_v1(
        "Alpha",
        whitelist=whitelist,
        client=client,
        content_timeout_seconds=0.01,
    )
    elapsed = time.perf_counter() - started

    assert elapsed < 0.5
    assert client.table_calls == [("base_alpha_slow", "tbl_alpha_slow")]
    assert len(hits) == 1
    hit = hits[0]
    assert hit.matched_field == "meta"
    assert hit.matched_snippet == ""
    assert hit.metadata["retrieval_mode"] == "metadata_only"
    assert hit.metadata["content_status"] == "error"
    assert hit.metadata["error_type"] == "TimeoutError"


@pytest.mark.asyncio
async def test_production_client_document_block_error_reaches_query_metadata() -> None:
    whitelist = Whitelist(
        documents=[
            WhitelistDocument(
                doc_token="doxc_alpha",
                name="Alpha incident runbook",
                domain="ops",
                description="Escalation steps",
            )
        ]
    )

    class DocumentApi:
        async def aget(self, _request):
            return _FakeLarkResponse(
                ok=True,
                data=SimpleNamespace(
                    document=SimpleNamespace(title="Remote title"),
                ),
            )

    class BlockApi:
        async def alist(self, _request):
            return _FakeLarkResponse(
                ok=False,
                code="docx_block_failed",
                msg="block API unavailable",
            )

    client = _make_production_client(
        _make_lark_client(document_api=DocumentApi(), block_api=BlockApi())
    )

    hits = await query_resources_v1("Alpha", whitelist=whitelist, client=client)

    assert len(hits) == 1
    hit = hits[0]
    assert hit.matched_field == "meta"
    assert hit.metadata["retrieval_mode"] == "metadata_only"
    assert hit.metadata["content_status"] == "error"
    assert hit.metadata["error_type"] == "FeishuClientError"


@pytest.mark.asyncio
async def test_production_client_bitable_error_reaches_query_metadata() -> None:
    whitelist = Whitelist(
        tables=[
            WhitelistTable(
                app_token="base_alpha",
                table_id="tbl_alpha",
                name="Alpha account tracker",
                domain="ops",
                description="Customer records",
            )
        ]
    )

    class RecordApi:
        async def alist(self, _request):
            return _FakeLarkResponse(
                ok=False,
                code="bitable_failed",
                msg="records API unavailable",
            )

    client = _make_production_client(_make_lark_client(record_api=RecordApi()))

    hits = await query_resources_v1("Alpha", whitelist=whitelist, client=client)

    assert len(hits) == 1
    hit = hits[0]
    assert hit.matched_field == "meta"
    assert hit.metadata["retrieval_mode"] == "metadata_only"
    assert hit.metadata["content_status"] == "error"
    assert hit.metadata["error_type"] == "FeishuClientError"


@pytest.mark.asyncio
async def test_production_client_raises_on_partial_bitable_page_error() -> None:
    class RecordApi:
        def __init__(self) -> None:
            self.calls = 0

        async def alist(self, _request):
            self.calls += 1
            if self.calls == 1:
                return _FakeLarkResponse(
                    ok=True,
                    data=SimpleNamespace(
                        items=[
                            SimpleNamespace(
                                record_id="rec_1",
                                fields={"status": "Alpha matched"},
                            )
                        ],
                        has_more=True,
                        page_token="next_page",
                    ),
                )
            return _FakeLarkResponse(
                ok=False,
                code="bitable_page_failed",
                msg="next page unavailable",
            )

    client = _make_production_client(_make_lark_client(record_api=RecordApi()))

    with pytest.raises(FeishuClientError):
        await client.read_table_records("base_alpha", "tbl_alpha", limit=500)


@pytest.mark.asyncio
async def test_v1_bounds_content_api_fanout_for_large_whitelist() -> None:
    whitelist = Whitelist(
        documents=[
            WhitelistDocument(
                doc_token=f"doxc_{index}",
                name=f"Needle doc {index}" if index < 4 else f"Doc {index}",
                domain="ops",
                description="Runbook",
            )
            for index in range(20)
        ],
        tables=[
            WhitelistTable(
                app_token=f"base_{index}",
                table_id=f"tbl_{index}",
                name=f"Needle table {index}",
                domain="ops",
                description="Tracker",
            )
            for index in range(20)
        ],
    )
    client = FakeFeishuClient()

    hits = await query_resources_v1(
        "Needle",
        whitelist=whitelist,
        client=client,
        limit=5,
        max_sources=6,
    )

    assert len(client.document_calls) + len(client.table_calls) == 6
    assert client.document_calls == ["doxc_0", "doxc_1", "doxc_2", "doxc_3"]
    assert client.table_calls == [("base_0", "tbl_0"), ("base_1", "tbl_1")]
    assert len(hits) == 5


@pytest.mark.asyncio
async def test_empty_document_content_uses_empty_content_status() -> None:
    whitelist = Whitelist(
        documents=[
            WhitelistDocument(
                doc_token="doxc_alpha_empty",
                name="Alpha empty runbook",
                domain="ops",
                description="Escalation steps",
            )
        ]
    )
    client = FakeFeishuClient(
        documents={
            "doxc_alpha_empty": DocContent(
                doc_token="doxc_alpha_empty",
                title="",
                plain_text="",
                block_count=0,
            )
        }
    )

    hits = await query_resources_v1("Alpha", whitelist=whitelist, client=client)

    assert len(hits) == 1
    hit = hits[0]
    assert hit.matched_field == "meta"
    assert hit.matched_snippet == ""
    assert hit.metadata["retrieval_mode"] == "metadata_only"
    assert hit.metadata["content_status"] == "empty"


@pytest.mark.asyncio
async def test_empty_table_records_use_empty_records_content_status() -> None:
    whitelist = Whitelist(
        tables=[
            WhitelistTable(
                app_token="base_alpha",
                table_id="tbl_alpha_empty",
                name="Alpha empty table",
                domain="ops",
                description="No rows yet",
            )
        ]
    )
    client = FakeFeishuClient(tables={("base_alpha", "tbl_alpha_empty"): []})

    hits = await query_resources_v1("Alpha", whitelist=whitelist, client=client)

    assert len(hits) == 1
    hit = hits[0]
    assert hit.matched_field == "meta"
    assert hit.matched_snippet == ""
    assert hit.metadata["retrieval_mode"] == "metadata_only"
    assert hit.metadata["content_status"] == "empty_records"
