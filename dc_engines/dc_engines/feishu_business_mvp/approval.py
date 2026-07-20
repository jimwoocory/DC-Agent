"""Feishu approval synchronization helpers for finance workflows."""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from io import BytesIO
from typing import Any

from dc_engines.feishu_hub import call as hub_call
from dc_engines.feishu_hub import get_client, is_enabled

from .contracts import FinanceApprovalRecord
from .store import BusinessMvpStore

CallFn = Callable[[str, Awaitable[Any]], Awaitable[Any]]
AttachmentFetcher = Callable[[str], Awaitable[bytes]]
InvoiceTextExtractor = Callable[[bytes], str]


def assess_attachment_status(
    fields: dict[str, Any],
    required_labels: list[str],
) -> tuple[str, list[str]]:
    """Check whether required finance attachments are present.

    Args:
        fields: Normalized approval fields or Bitable fields.
        required_labels: Required labels such as 发票, 合同, or 付款截图.

    Returns:
        Tuple of attachment_status and missing attachment labels.
    """

    if not required_labels:
        return "unknown", []

    missing: list[str] = []
    for label in required_labels:
        needle = label.strip().lower()
        if not needle:
            continue
        found = False
        for key, value in fields.items():
            key_text = str(key or "").lower()
            value_text = _flatten_text(value).lower()
            if needle in key_text:
                found = _has_value(value)
                break
            if needle in value_text and _has_value(value):
                found = True
                break
        if not found and needle in {"发票", "invoice"}:
            found = any(
                "附件" in str(key or "").lower() and _has_value(value)
                for key, value in fields.items()
            )
        if not found:
            missing.append(label)

    return ("complete", []) if not missing else ("missing", missing)


class ApprovalSyncer:
    """Sync Feishu approval instances into finance records through feishu_hub."""

    def __init__(
        self,
        *,
        store: BusinessMvpStore | None = None,
        client: Any | None = None,
        enabled: bool | None = None,
        call_fn: CallFn | None = None,
        required_attachments: dict[str, list[str]] | None = None,
        attachment_fetcher: AttachmentFetcher | None = None,
        invoice_text_extractor: InvoiceTextExtractor | None = None,
    ) -> None:
        """Create an approval syncer.

        Args:
            store: Optional local store used to persist synced records.
            client: Optional injected lark client for tests.
            enabled: Optional enabled flag. Defaults to feishu_hub.is_enabled().
            call_fn: Optional injected async call wrapper. Defaults to feishu_hub.call.
            required_attachments: Required attachment labels by approval type.
            attachment_fetcher: Optional attachment downloader used for invoice review.
            invoice_text_extractor: Optional local PDF text/OCR extractor.
        """

        self.store = store
        self._client = client if client is not None else get_client()
        self._enabled = bool(is_enabled() if enabled is None else enabled)
        self._call = call_fn or hub_call
        self.required_attachments = required_attachments or {}
        self._attachment_fetcher = attachment_fetcher or _download_attachment
        self._invoice_text_extractor = invoice_text_extractor or _extract_pdf_text

    @property
    def enabled(self) -> bool:
        """Return whether live approval API calls can be issued.

        Returns:
            True when enabled and a client is available.
        """

        return bool(self._enabled and self._client is not None)

    async def list_instance_codes(
        self,
        approval_code: str,
        *,
        start_time: int,
        end_time: int,
        limit: int = 500,
    ) -> list[str]:
        """List approval instance codes by approval code and time window.

        Args:
            approval_code: Feishu approval code.
            start_time: Window start in Feishu timestamp units.
            end_time: Window end in Feishu timestamp units.
            limit: Maximum codes to return.

        Returns:
            Approval instance codes, or an empty list when disabled.
        """

        if not self.enabled or not approval_code:
            return []

        from lark_oapi.api.approval.v4 import ListInstanceRequest

        out: list[str] = []
        page_token = ""
        while len(out) < limit:
            builder = (
                ListInstanceRequest.builder()
                .approval_code(approval_code)
                .start_time(start_time)
                .end_time(end_time)
                .page_size(min(100, limit - len(out)))
            )
            if page_token:
                builder = builder.page_token(page_token)
            req = builder.build()
            resp = await self._call(
                "approval.instance.list",
                self._client.approval.v4.instance.alist(req),
            )
            _raise_if_failed(resp, "approval instance list")
            data = getattr(resp, "data", None)
            for code in _extract_instance_codes(data):
                if code not in out:
                    out.append(code)
                if len(out) >= limit:
                    break
            if not getattr(data, "has_more", False):
                break
            page_token = str(getattr(data, "page_token", "") or "")
            if not page_token:
                break
        return out

    async def get_instance(self, instance_code: str) -> dict[str, Any] | None:
        """Fetch one approval instance detail.

        Args:
            instance_code: Feishu approval instance code or ID.

        Returns:
            Normalized dict detail, or None when disabled.
        """

        if not self.enabled or not instance_code:
            return None

        from lark_oapi.api.approval.v4 import GetInstanceRequest

        req = GetInstanceRequest.builder().instance_id(instance_code).build()
        resp = await self._call(
            "approval.instance.get",
            self._client.approval.v4.instance.aget(req),
        )
        _raise_if_failed(resp, "approval instance get")
        data = getattr(resp, "data", None)
        instance = getattr(data, "instance", None) or data
        return _to_plain_dict(instance)

    async def sync_finance_approvals(
        self,
        approval_code: str,
        approval_type: str,
        *,
        start_time: int,
        end_time: int,
        limit: int = 500,
    ) -> list[FinanceApprovalRecord]:
        """Sync approval instances into local finance records.

        Args:
            approval_code: Feishu approval code.
            approval_type: Local approval type label.
            start_time: Window start in Feishu timestamp units.
            end_time: Window end in Feishu timestamp units.
            limit: Maximum instances to sync.

        Returns:
            Normalized finance records.
        """

        codes = await self.list_instance_codes(
            approval_code,
            start_time=start_time,
            end_time=end_time,
            limit=limit,
        )
        records: list[FinanceApprovalRecord] = []
        for code in codes:
            detail = await self.get_instance(code)
            if detail is None:
                continue
            record = self.finance_record_from_detail(code, approval_type, detail)
            record.metadata["invoice_review"] = await self._review_invoice_record(
                record
            )
            records.append(record)
            if self.store is not None:
                await self.store.upsert_finance_record(record)
        return records

    def finance_record_from_detail(
        self,
        instance_code: str,
        approval_type: str,
        detail: dict[str, Any],
    ) -> FinanceApprovalRecord:
        """Normalize approval detail into a finance approval record.

        Args:
            instance_code: Feishu approval instance code.
            approval_type: Local approval type label.
            detail: Approval detail dict.

        Returns:
            FinanceApprovalRecord.
        """

        fields = _extract_form_fields(detail)
        required = self.required_attachments.get(approval_type, [])
        attachment_status, missing = assess_attachment_status(fields, required)
        return FinanceApprovalRecord(
            approval_instance_code=instance_code,
            approval_type=approval_type,
            amount=_extract_amount(fields),
            department=_first_text(fields, ["部门", "department", "dept"]),
            applicant_id=_first_text(detail, ["applicant_id", "user_id", "open_id"]),
            applicant_name=_first_text(
                fields, ["申请人", "报销人", "applicant", "name"]
            ),
            status=_first_text(detail, ["status", "instance_status"]) or "unknown",
            attachment_status=attachment_status,
            missing_attachments=missing,
            approved_at=_first_text(detail, ["approved_at", "end_time", "finish_time"]),
            metadata={"approval_detail": detail, "form_fields": fields},
        )

    async def _review_invoice_record(
        self,
        record: FinanceApprovalRecord,
    ) -> dict[str, Any]:
        """Extract invoice identifiers and prepare a non-mutating finance review.

        Args:
            record: Normalized reimbursement record with attachment fields.

        Returns:
            Invoice OCR data, duplicate candidates, and manual-review reasons.
        """

        fields = dict(record.metadata.get("form_fields") or {})
        attachment_urls = [
            value
            for value in re.findall(r"https?://[^\s\]\"']+", _flatten_text(fields))
            if value.lower().split("?", 1)[0].endswith(".pdf")
            or "download" in value.lower()
        ]
        invoices: list[dict[str, Any]] = []
        reasons: list[str] = []
        for url in dict.fromkeys(attachment_urls):
            try:
                text = self._invoice_text_extractor(await self._attachment_fetcher(url))
            except Exception:  # noqa: BLE001
                reasons.append("invoice_download_or_ocr_failed")
                continue
            normalized = re.sub(r"\s+", " ", text)
            number_match = re.search(
                r"发票号码[：:]?\s*([A-Z0-9-]{8,})", normalized, re.IGNORECASE
            )
            if number_match is None:
                number_match = re.search(r"(?<!\d)(\d{20})(?!\d)", text)
            amount_match = re.search(
                r"价税合计.*?[¥￥]\s*([0-9,]+(?:\.\d{1,2})?)",
                normalized,
            )
            invoice = {
                "invoice_number": number_match.group(1).replace(" ", "")
                if number_match
                else "",
                "invoice_amount": float(amount_match.group(1).replace(",", ""))
                if amount_match
                else None,
            }
            invoices.append(invoice)
            if not invoice["invoice_number"]:
                reasons.append("invoice_number_unreadable")
            if invoice["invoice_amount"] is None:
                reasons.append("invoice_amount_unreadable")

        invoice_numbers = {
            str(invoice["invoice_number"])
            for invoice in invoices
            if invoice["invoice_number"]
        }
        duplicates: list[str] = []
        if self.store is not None and invoice_numbers:
            for existing in await self.store.list_finance_records(limit=5000):
                if existing.approval_instance_code == record.approval_instance_code:
                    continue
                existing_review = dict(existing.metadata.get("invoice_review") or {})
                existing_numbers = {
                    str(invoice.get("invoice_number") or "")
                    for invoice in list(existing_review.get("invoices") or [])
                }
                if invoice_numbers & existing_numbers:
                    duplicates.append(existing.approval_instance_code)
        if duplicates:
            reasons.append("duplicate_invoice")

        invoice_total = sum(
            float(invoice["invoice_amount"])
            for invoice in invoices
            if invoice["invoice_amount"] is not None
        )
        if invoices and all(
            invoice["invoice_amount"] is not None for invoice in invoices
        ):
            if abs(record.amount - invoice_total) > 0.01:
                reasons.append("invoice_total_differs_from_reimbursement_total")
        elif not invoices:
            reasons.append("invoice_attachment_not_found")
        return {
            "status": "manual_review" if reasons else "ready_for_finance_review",
            "reasons": list(dict.fromkeys(reasons)),
            "reimbursement_amount": record.amount,
            "invoice_total": invoice_total if invoices else None,
            "invoices": invoices,
            "duplicate_instance_codes": duplicates,
        }


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


def _extract_instance_codes(data: Any) -> list[str]:
    """Extract instance codes from Feishu list response data.

    Args:
        data: Feishu response data.

    Returns:
        Instance code strings.
    """

    candidates = (
        getattr(data, "instance_code_list", None)
        or getattr(data, "instance_codes", None)
        or getattr(data, "items", None)
        or []
    )
    out: list[str] = []
    for item in candidates:
        if isinstance(item, str):
            out.append(item)
            continue
        if isinstance(item, dict):
            value = (
                item.get("instance_code") or item.get("instance_id") or item.get("id")
            )
        else:
            value = (
                getattr(item, "instance_code", None)
                or getattr(item, "instance_id", None)
                or getattr(item, "id", None)
            )
        if value:
            out.append(str(value))
    return out


def _extract_form_fields(detail: dict[str, Any]) -> dict[str, Any]:
    """Extract approval form fields from heterogeneous Feishu payloads.

    Args:
        detail: Approval detail.

    Returns:
        Flattened field map.
    """

    fields: dict[str, Any] = {}
    form = (
        detail.get("form")
        or detail.get("form_fields")
        or detail.get("form_component_values")
        or detail.get("field_list")
        or detail.get("approval_detail")
    )
    parsed_form = _parse_jsonish(form)
    if isinstance(parsed_form, list):
        for index, item in enumerate(parsed_form):
            if isinstance(item, dict):
                name = str(
                    item.get("name")
                    or item.get("label")
                    or item.get("field_name")
                    or item.get("id")
                    or f"field_{index}"
                )
                fields[name] = item.get("value", item.get("field_value", item))
            else:
                fields[f"field_{index}"] = item
    elif isinstance(parsed_form, dict):
        for key, value in parsed_form.items():
            fields[str(key)] = value

    if not fields:
        for key, value in detail.items():
            if isinstance(value, str | int | float | bool | list | dict):
                fields[str(key)] = value
    return fields


def _extract_amount(fields: dict[str, Any]) -> float:
    """Extract a finance amount from fields.

    Args:
        fields: Approval form fields.

    Returns:
        Amount as float, defaulting to 0.
    """

    for needle in [
        "费用汇总",
        "总金额",
        "金额合计",
        "合计",
        "金额",
        "amount",
        "付款金额",
    ]:
        for key, value in fields.items():
            if needle not in str(key).lower() or not _has_value(value):
                continue
            cleaned = "".join(
                ch for ch in _flatten_text(value) if ch.isdigit() or ch in ".-"
            )
            try:
                return float(cleaned) if cleaned else 0.0
            except ValueError:
                continue
    return 0.0


def _first_text(payload: dict[str, Any], keys: list[str]) -> str:
    """Find the first matching text value by fuzzy key names.

    Args:
        payload: Dict to search.
        keys: Lowercase or Chinese key fragments.

    Returns:
        First matching flattened text value.
    """

    needles = [key.lower() for key in keys]
    for key, value in payload.items():
        key_text = str(key).lower()
        if any(needle in key_text for needle in needles) and _has_value(value):
            return _flatten_text(value)
    for value in payload.values():
        parsed = _parse_jsonish(value)
        if isinstance(parsed, dict):
            found = _first_text(parsed, keys)
            if found:
                return found
    return ""


def _has_value(value: Any) -> bool:
    """Return whether a field has meaningful content.

    Args:
        value: Field value.

    Returns:
        True when the value is non-empty.
    """

    if value is None:
        return False
    if isinstance(value, str):
        text = value.strip().lower()
        return bool(text and text not in {"[]", "{}", "null", "none", "无"})
    if isinstance(value, list | tuple | set):
        return any(_has_value(item) for item in value)
    if isinstance(value, dict):
        return any(_has_value(item) for item in value.values())
    return True


def _flatten_text(value: Any) -> str:
    """Flatten nested values into text for fuzzy matching.

    Args:
        value: Any value.

    Returns:
        Text representation.
    """

    parsed = _parse_jsonish(value)
    if isinstance(parsed, dict):
        return " ".join(
            f"{key} {_flatten_text(item)}" for key, item in parsed.items()
        ).strip()
    if isinstance(parsed, list | tuple | set):
        return " ".join(_flatten_text(item) for item in parsed).strip()
    return str(parsed or "").strip()


def _parse_jsonish(value: Any) -> Any:
    """Parse JSON strings and return other values unchanged.

    Args:
        value: Any value.

    Returns:
        Parsed JSON or the original value.
    """

    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text or text[0] not in "[{":
        return value
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return value


async def _download_attachment(url: str) -> bytes:
    """Download a temporary Feishu attachment URL for local processing.

    Args:
        url: Temporary attachment download URL from an approval form.

    Returns:
        Attachment bytes.

    Raises:
        RuntimeError: If the response is not a successful PDF download.
    """

    import httpx

    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        response = await client.get(url)
    response.raise_for_status()
    if "pdf" not in response.headers.get("content-type", "").lower():
        raise RuntimeError("invoice attachment is not a PDF")
    return response.content


def _extract_pdf_text(content: bytes) -> str:
    """Extract searchable text from an invoice PDF using local libraries.

    Args:
        content: PDF bytes downloaded from the approval attachment.

    Returns:
        Extracted text, or an empty string when OCR is required.
    """

    try:
        from pypdf import PdfReader

        return "\n".join(
            page.extract_text() or "" for page in PdfReader(BytesIO(content)).pages
        )
    except Exception:  # noqa: BLE001
        return ""


def _to_plain_dict(value: Any) -> dict[str, Any]:
    """Convert SDK approval values to plain dicts.

    Args:
        value: SDK object, dict, list, or scalar.

    Returns:
        Plain dict representation.
    """

    if value is None:
        return {}
    if isinstance(value, dict):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    if hasattr(value, "__dict__"):
        return {
            str(key): _to_jsonable(item)
            for key, item in vars(value).items()
            if not str(key).startswith("_")
        }
    return {"value": _to_jsonable(value)}


def _to_jsonable(value: Any) -> Any:
    """Convert SDK values to JSON-friendly values.

    Args:
        value: Any value.

    Returns:
        JSON-friendly value.
    """

    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, list | tuple):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    if hasattr(value, "__dict__"):
        return _to_plain_dict(value)
    return str(value)
