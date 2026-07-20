"""Feishu Bitable adapter for business MVP workflows."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from dc_engines.feishu_hub import call as hub_call
from dc_engines.feishu_hub import get_client, is_enabled

from .contracts import BitableLocation

CallFn = Callable[[str, Awaitable[Any]], Awaitable[Any]]


@dataclass(slots=True)
class BitableRecord:
    """A normalized Feishu Bitable record.

    Args:
        record_id: Feishu record ID.
        fields: Record fields.
        raw: Raw SDK object converted as much as possible.
    """

    record_id: str
    fields: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


class FeishuBitableClient:
    """Small read/write wrapper around Feishu Bitable APIs through feishu_hub."""

    def __init__(
        self,
        *,
        client: Any | None = None,
        enabled: bool | None = None,
        call_fn: CallFn | None = None,
    ) -> None:
        """Create a Bitable adapter.

        Args:
            client: Optional injected lark client for tests.
            enabled: Optional enabled flag. Defaults to feishu_hub.is_enabled().
            call_fn: Optional injected async call wrapper. Defaults to feishu_hub.call.
        """

        self._client = client if client is not None else get_client()
        self._enabled = bool(is_enabled() if enabled is None else enabled)
        self._call = call_fn or hub_call

    @property
    def enabled(self) -> bool:
        """Return whether the adapter can issue live Feishu calls.

        Returns:
            True when enabled and a client is available.
        """

        return bool(self._enabled and self._client is not None)

    async def list_records(
        self,
        location: BitableLocation,
        *,
        limit: int = 500,
    ) -> list[BitableRecord]:
        """List Bitable records with pagination.

        Args:
            location: Bitable table location.
            limit: Maximum number of records to return.

        Returns:
            Normalized records, or an empty list when disabled.
        """

        if not self.enabled or not location.configured:
            return []

        from lark_oapi.api.bitable.v1 import ListAppTableRecordRequest

        out: list[BitableRecord] = []
        page_token = ""
        while len(out) < limit:
            builder = (
                ListAppTableRecordRequest.builder()
                .app_token(location.app_token)
                .table_id(location.table_id)
                .page_size(min(200, limit - len(out)))
            )
            if location.view_id and hasattr(builder, "view_id"):
                builder = builder.view_id(location.view_id)
            if page_token:
                builder = builder.page_token(page_token)
            req = builder.build()
            resp = await self._call(
                "bitable.app_table_record.list",
                self._client.bitable.v1.app_table_record.alist(req),
            )
            self._raise_if_failed(resp, "bitable list records")
            data = getattr(resp, "data", None)
            for item in getattr(data, "items", None) or []:
                out.append(self._normalize_record(item))
                if len(out) >= limit:
                    break
            if not getattr(data, "has_more", False):
                break
            page_token = str(getattr(data, "page_token", "") or "")
            if not page_token:
                break
        return out

    async def search_records(
        self,
        location: BitableLocation,
        *,
        filter_body: dict[str, Any] | None = None,
        field_names: list[str] | None = None,
        limit: int = 200,
    ) -> list[BitableRecord]:
        """Search Bitable records.

        Args:
            location: Bitable table location.
            filter_body: Optional Feishu filter body.
            field_names: Optional field names returned by Feishu.
            limit: Maximum number of records to return.

        Returns:
            Matching normalized records, or an empty list when disabled.
        """

        if not self.enabled or not location.configured:
            return []

        from lark_oapi.api.bitable.v1 import (
            SearchAppTableRecordRequest,
            SearchAppTableRecordRequestBody,
        )

        body_builder = SearchAppTableRecordRequestBody.builder()
        if location.view_id:
            body_builder = body_builder.view_id(location.view_id)
        if field_names:
            body_builder = body_builder.field_names(field_names)
        if filter_body:
            body_builder = body_builder.filter(filter_body)

        out: list[BitableRecord] = []
        page_token = ""
        while len(out) < limit:
            builder = (
                SearchAppTableRecordRequest.builder()
                .app_token(location.app_token)
                .table_id(location.table_id)
                .page_size(min(200, limit - len(out)))
                .request_body(body_builder.build())
            )
            if page_token:
                builder = builder.page_token(page_token)
            req = builder.build()
            resp = await self._call(
                "bitable.app_table_record.search",
                self._client.bitable.v1.app_table_record.asearch(req),
            )
            self._raise_if_failed(resp, "bitable search records")
            data = getattr(resp, "data", None)
            for item in getattr(data, "items", None) or []:
                out.append(self._normalize_record(item))
                if len(out) >= limit:
                    break
            if not getattr(data, "has_more", False):
                break
            page_token = str(getattr(data, "page_token", "") or "")
            if not page_token:
                break
        return out

    async def create_record(
        self,
        location: BitableLocation,
        fields: dict[str, Any],
    ) -> BitableRecord | None:
        """Create a Bitable record.

        Args:
            location: Bitable table location.
            fields: Record fields to create.

        Returns:
            Created record, or None when disabled/unconfigured.
        """

        if not self.enabled or not location.configured:
            return None

        from lark_oapi.api.bitable.v1 import AppTableRecord, CreateAppTableRecordRequest

        body = AppTableRecord.builder().fields(fields).build()
        req = (
            CreateAppTableRecordRequest.builder()
            .app_token(location.app_token)
            .table_id(location.table_id)
            .request_body(body)
            .build()
        )
        resp = await self._call(
            "bitable.app_table_record.create",
            self._client.bitable.v1.app_table_record.acreate(req),
        )
        self._raise_if_failed(resp, "bitable create record")
        return self._normalize_write_response(resp)

    async def update_record(
        self,
        location: BitableLocation,
        record_id: str,
        fields: dict[str, Any],
    ) -> BitableRecord | None:
        """Update a Bitable record.

        Args:
            location: Bitable table location.
            record_id: Feishu Bitable record ID.
            fields: Record fields to patch.

        Returns:
            Updated record, or None when disabled/unconfigured.
        """

        if not self.enabled or not location.configured or not record_id:
            return None

        from lark_oapi.api.bitable.v1 import AppTableRecord, UpdateAppTableRecordRequest

        body = AppTableRecord.builder().fields(fields).build()
        req = (
            UpdateAppTableRecordRequest.builder()
            .app_token(location.app_token)
            .table_id(location.table_id)
            .record_id(record_id)
            .request_body(body)
            .build()
        )
        resp = await self._call(
            "bitable.app_table_record.update",
            self._client.bitable.v1.app_table_record.aupdate(req),
        )
        self._raise_if_failed(resp, "bitable update record")
        return self._normalize_write_response(resp)

    @staticmethod
    def _raise_if_failed(resp: Any, label: str) -> None:
        """Raise RuntimeError for failed SDK responses.

        Args:
            resp: Feishu SDK response.
            label: Human-readable operation label.

        Raises:
            RuntimeError: If resp.success() returns False.
        """

        success = getattr(resp, "success", None)
        if callable(success) and success():
            return
        if success is None:
            return
        code = getattr(resp, "code", "?")
        msg = getattr(resp, "msg", "")
        raise RuntimeError(f"{label} failed code={code} msg={msg}")

    def _normalize_write_response(self, resp: Any) -> BitableRecord:
        """Normalize create/update responses.

        Args:
            resp: Feishu SDK response.

        Returns:
            Normalized BitableRecord.
        """

        data = getattr(resp, "data", None)
        record = (
            getattr(data, "record", None)
            or getattr(data, "app_table_record", None)
            or data
        )
        return self._normalize_record(record)

    def _normalize_record(self, record: Any) -> BitableRecord:
        """Normalize an SDK or dict record.

        Args:
            record: Feishu SDK record or dict.

        Returns:
            Normalized BitableRecord.
        """

        raw = self._to_plain_dict(record)
        if isinstance(record, dict):
            record_id = str(record.get("record_id") or record.get("id") or "")
            fields = record.get("fields") or {}
        else:
            record_id = str(getattr(record, "record_id", "") or "")
            fields = getattr(record, "fields", None) or {}
        if not isinstance(fields, dict):
            fields = self._to_plain_dict(fields)
        return BitableRecord(record_id=record_id, fields=dict(fields), raw=raw)

    def _to_plain_dict(self, value: Any) -> dict[str, Any]:
        """Best-effort conversion of SDK objects to plain dicts.

        Args:
            value: Any SDK object, dict, list, or scalar.

        Returns:
            Dict representation when possible.
        """

        if value is None:
            return {}
        if isinstance(value, dict):
            return {str(k): self._to_jsonable(v) for k, v in value.items()}
        if hasattr(value, "__dict__"):
            return {
                str(k): self._to_jsonable(v)
                for k, v in vars(value).items()
                if not str(k).startswith("_")
            }
        return {"value": self._to_jsonable(value)}

    def _to_jsonable(self, value: Any) -> Any:
        """Convert SDK values to JSON-friendly values.

        Args:
            value: Any value.

        Returns:
            JSON-friendly value.
        """

        if value is None or isinstance(value, str | int | float | bool):
            return value
        if isinstance(value, list | tuple):
            return [self._to_jsonable(item) for item in value]
        if isinstance(value, dict):
            return {str(k): self._to_jsonable(v) for k, v in value.items()}
        if hasattr(value, "__dict__"):
            return self._to_plain_dict(value)
        return str(value)
