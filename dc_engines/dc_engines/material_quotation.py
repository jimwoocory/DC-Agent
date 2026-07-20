"""Deterministic material-price matching and quotation calculations."""

from __future__ import annotations

import json
import re
import sqlite3
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from nas_sync.dc_memory_indexer import (
    dedupe_query_rows,
    fetch_fts_rows,
    fetch_like_rows,
    query_terms,
)

MONEY = Decimal("0.01")
MAX_QUOTATION_ITEMS = 20
MAX_QUOTATION_IMAGES_PER_ITEM = 6


def search_material_prices(
    query: str,
    *,
    db_path: str | Path,
    limit: int = 10,
) -> list[dict[str, str]]:
    """Search indexed Obsidian supplier tables for structured historical prices.

    Args:
        query: Material, specification, or supplier text entered in Feishu.
        db_path: Path to the NAS and Obsidian search index.
        limit: Maximum number of structured price matches to return.

    Returns:
        Ranked price records containing item, specification, unit price,
        supplier, source path, and review status.

    Raises:
        ValueError: If the query is empty or unreasonably long.
    """
    normalized_query = re.sub(r"\s+", " ", str(query or "")).strip()
    if not normalized_query or len(normalized_query) > 80:
        raise ValueError("请输入 1-80 个字符的物料名称或规格")
    database = Path(db_path)
    if not database.is_file():
        return []

    terms = query_terms(f"{normalized_query} 报价")
    fetch_limit = max(limit * 40, 200)
    with sqlite3.connect(database) as conn:
        conn.row_factory = sqlite3.Row
        rows = fetch_fts_rows(conn, normalized_query, fetch_limit)
        rows.extend(fetch_like_rows(conn, terms, fetch_limit))
    ranked_documents = dedupe_query_rows(
        rows,
        terms,
        f"{normalized_query} 报价",
        max(limit * 4, 20),
    )

    matches: list[dict[str, str]] = []
    query_key = normalized_query.casefold()
    for document in ranked_documents:
        lines = str(document.get("text") or "").splitlines()
        for line_index in range(len(lines) - 2):
            header_line = lines[line_index].strip()
            separator_line = lines[line_index + 1].strip()
            if not (header_line.startswith("|") and separator_line.startswith("|")):
                continue
            headers = [cell.strip() for cell in header_line.strip("|").split("|")]
            separators = [cell.strip() for cell in separator_line.strip("|").split("|")]
            if len(headers) != len(separators) or not all(
                re.fullmatch(r":?-{3,}:?", cell) for cell in separators
            ):
                continue
            item_index = next(
                (
                    index
                    for index, header in enumerate(headers)
                    if any(
                        marker in header
                        for marker in ("项目", "材料", "物料", "方案", "产品")
                    )
                ),
                -1,
            )
            price_index = next(
                (
                    index
                    for index, header in enumerate(headers)
                    if "单价" in header or header.strip() == "价格"
                ),
                -1,
            )
            if item_index < 0 or price_index < 0:
                continue
            spec_index = next(
                (
                    index
                    for index, header in enumerate(headers)
                    if any(
                        marker in header
                        for marker in (
                            "规格",
                            "说明",
                            "材质",
                            "工艺",
                            "主要内容",
                            "备注",
                        )
                    )
                ),
                -1,
            )
            quantity_index = next(
                (index for index, header in enumerate(headers) if "数量" in header),
                -1,
            )
            unit_index = next(
                (
                    index
                    for index, header in enumerate(headers)
                    if "单位" in header and index != price_index
                ),
                -1,
            )
            header_unit_match = re.search(r"元\s*/\s*([^）)]+)", headers[price_index])
            try:
                metadata = json.loads(str(document.get("metadata_json") or "{}"))
            except (json.JSONDecodeError, TypeError):
                metadata = {}
            metadata_supplier = str(metadata.get("supplier") or "").strip()
            supplier = (metadata_supplier or str(document.get("title") or "历史报价"))[
                :80
            ]
            if not metadata_supplier:
                for heading_line in reversed(lines[:line_index]):
                    if not heading_line.lstrip().startswith("#"):
                        continue
                    heading = heading_line.lstrip("# ").strip()
                    if any(
                        marker in heading
                        for marker in ("公司", "集团", "广告", "供应商")
                    ):
                        supplier = re.sub(r"(?:可见)?报价$", "", heading).strip()[:80]
                    break
            row_index = line_index + 2
            while row_index < len(lines):
                row_line = lines[row_index].strip()
                if not row_line.startswith("|"):
                    break
                cells = [cell.strip() for cell in row_line.strip("|").split("|")]
                row_index += 1
                if len(cells) != len(headers):
                    continue
                item_name = re.sub(r"[*_`]", "", cells[item_index]).strip()
                price_match = re.search(
                    r"(?<![\d.])([0-9][0-9,]*(?:\.[0-9]+)?)",
                    cells[price_index],
                )
                if not item_name or price_match is None:
                    continue
                searchable = " ".join(cells).casefold()
                if query_key not in searchable and not all(
                    term.casefold() in searchable
                    for term in query_terms(normalized_query)[:3]
                ):
                    continue
                try:
                    unit_price = Decimal(price_match.group(1).replace(",", ""))
                except InvalidOperation:
                    continue
                specification = cells[spec_index] if spec_index >= 0 else ""
                specification = "" if specification == "-" else specification
                unit = cells[unit_index].strip() if unit_index >= 0 else ""
                if not unit:
                    unit = (
                        header_unit_match.group(1).strip() if header_unit_match else ""
                    )
                if not unit:
                    cell_unit_match = re.search(
                        r"元\s*/\s*([\u4e00-\u9fff㎡²]+)", cells[price_index]
                    )
                    if cell_unit_match:
                        unit = cell_unit_match.group(1)
                if not unit and quantity_index >= 0:
                    quantity_unit_match = re.search(
                        r"[0-9,.]+\s*([\u4e00-\u9fff㎡²]+)",
                        cells[quantity_index],
                    )
                    if quantity_unit_match:
                        unit = quantity_unit_match.group(1)
                if unit in {"-", "待确认", "待定", "未知"}:
                    unit = ""
                source_status = (
                    "已复核"
                    if str(document.get("review_status") or "") == "confirmed"
                    else "待复核"
                )
                matches.append(
                    {
                        "item_name": item_name[:80],
                        "specification": specification[:160],
                        "unit": unit[:16],
                        "unit_price": str(unit_price.quantize(MONEY)),
                        "supplier": supplier,
                        "source_path": str(
                            document.get("rel_path")
                            or document.get("source_path")
                            or ""
                        )[:300],
                        "source_status": source_status,
                    }
                )

    unique: dict[tuple[str, str, str, str], dict[str, str]] = {}
    for match in matches:
        key = (
            match["item_name"].casefold(),
            match["specification"].casefold(),
            match["supplier"].casefold(),
            match["unit_price"],
        )
        unique.setdefault(key, match)
    result = list(unique.values())
    result.sort(
        key=lambda item: (
            0 if item["item_name"].casefold() == query_key else 1,
            0 if query_key in item["item_name"].casefold() else 1,
            item["supplier"],
            Decimal(item["unit_price"]),
        )
    )
    return result[: max(1, min(int(limit), 20))]


def normalize_material_quotation(task_data: dict[str, Any]) -> dict[str, str]:
    """Validate quotation rows and recalculate every monetary field.

    Args:
        task_data: Raw project fields, serialized line items, and fee settings
            received from the Feishu sidebar.

    Returns:
        Sanitized string fields with normalized line items and server-computed
        subtotals, fees, tax, pending count, and grand total.

    Raises:
        ValueError: If required project or item data is missing, malformed, or
            outside the supported numeric ranges.
    """
    project_name = str(task_data.get("project_name") or "").strip()[:120]
    if not project_name:
        raise ValueError("请填写项目名称")
    try:
        raw_items = json.loads(str(task_data.get("quotation_items") or "[]"))
    except json.JSONDecodeError as exc:
        raise ValueError("物料明细格式无效") from exc
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("请至少添加一项物料")
    if len(raw_items) > MAX_QUOTATION_ITEMS:
        raise ValueError(f"一次最多填写 {MAX_QUOTATION_ITEMS} 项物料")

    def decimal_value(
        field_name: str,
        value: Any,
        *,
        maximum: Decimal,
        allow_empty: bool = False,
    ) -> Decimal | None:
        """Parse one bounded decimal used by the quotation calculator.

        Args:
            field_name: User-facing field name used in validation errors.
            value: Raw number received from the sidebar.
            maximum: Largest accepted value.
            allow_empty: Whether an empty value represents a pending price.

        Returns:
            A bounded Decimal, or None for an allowed empty value.

        Raises:
            ValueError: If the value is not numeric or outside its range.
        """
        cleaned = str(value if value is not None else "").replace(",", "").strip()
        if not cleaned and allow_empty:
            return None
        try:
            parsed = Decimal(cleaned or "0")
        except InvalidOperation as exc:
            raise ValueError(f"{field_name}必须是数字") from exc
        if parsed < 0 or parsed > maximum:
            raise ValueError(f"{field_name}超出允许范围")
        return parsed

    normalized_items: list[dict[str, Any]] = []
    priced_items_subtotal = Decimal("0")
    pending_count = 0
    for index, raw_item in enumerate(raw_items, start=1):
        if not isinstance(raw_item, dict):
            raise ValueError(f"第 {index} 项物料格式无效")
        item_name = str(raw_item.get("item_name") or "").strip()[:80]
        if not item_name:
            raise ValueError(f"请填写第 {index} 项物料名称")
        quantity = decimal_value(
            f"第 {index} 项数量",
            raw_item.get("quantity"),
            maximum=Decimal("1000000"),
        )
        if quantity is None or quantity <= 0:
            raise ValueError(f"第 {index} 项数量必须大于 0")
        unit = str(raw_item.get("unit") or "").strip()[:16]
        if not unit or unit in {"待确认", "待定", "未知", "-"}:
            raise ValueError(f"请填写第 {index} 项真实计价单位")
        unit_price = decimal_value(
            f"第 {index} 项单价",
            raw_item.get("unit_price"),
            maximum=Decimal("100000000"),
            allow_empty=True,
        )
        raw_image_paths = raw_item.get("image_paths")
        image_path_candidates = (
            raw_image_paths
            if isinstance(raw_image_paths, list)
            else [raw_image_paths]
            if raw_image_paths
            else []
        )
        legacy_image_path = str(raw_item.get("image_path") or "").strip()[:500]
        if legacy_image_path and legacy_image_path not in image_path_candidates:
            image_path_candidates.insert(0, legacy_image_path)
        image_paths: list[str] = []
        for candidate in image_path_candidates:
            normalized_path = str(candidate or "").strip()[:500]
            if normalized_path and normalized_path not in image_paths:
                image_paths.append(normalized_path)
        if len(image_paths) > MAX_QUOTATION_IMAGES_PER_ITEM:
            raise ValueError(
                f"第 {index} 项物料最多上传 {MAX_QUOTATION_IMAGES_PER_ITEM} 张图片"
            )
        normalized_item = {
            "item_name": item_name,
            "specification": str(raw_item.get("specification") or "").strip()[:160],
            "remark": str(raw_item.get("remark") or "").strip()[:300],
            "image_path": image_paths[0] if image_paths else "",
            "image_paths": image_paths,
            "quantity": str(quantity.normalize()),
            "unit": unit,
            "unit_price": "",
            "line_subtotal": "",
            "supplier": str(raw_item.get("supplier") or "").strip()[:80],
            "source_path": str(raw_item.get("source_path") or "").strip()[:300],
            "source_status": str(raw_item.get("source_status") or "").strip()[:40],
            "price_status": "待询价",
        }
        if unit_price is None:
            pending_count += 1
        else:
            line_subtotal = (quantity * unit_price).quantize(MONEY, ROUND_HALF_UP)
            priced_items_subtotal += line_subtotal
            normalized_item["unit_price"] = str(unit_price.quantize(MONEY))
            normalized_item["line_subtotal"] = str(line_subtotal)
            source_status = normalized_item["source_status"]
            normalized_item["price_status"] = (
                f"历史价{source_status}"
                if normalized_item["source_path"]
                else "人工录价"
            )[:40]
        normalized_items.append(normalized_item)

    transport_fee = decimal_value(
        "运输费", task_data.get("transport_fee"), maximum=Decimal("100000000")
    )
    installation_fee = decimal_value(
        "安装费", task_data.get("installation_fee"), maximum=Decimal("100000000")
    )
    rush_fee = decimal_value(
        "加急费", task_data.get("rush_fee"), maximum=Decimal("100000000")
    )
    loss_rate = decimal_value(
        "损耗率", task_data.get("loss_rate"), maximum=Decimal("100")
    )
    profit_rate = decimal_value(
        "利润率", task_data.get("profit_rate"), maximum=Decimal("100")
    )
    tax_rate = decimal_value("税率", task_data.get("tax_rate"), maximum=Decimal("100"))
    transport_fee = transport_fee or Decimal("0")
    installation_fee = installation_fee or Decimal("0")
    rush_fee = rush_fee or Decimal("0")
    loss_rate = loss_rate or Decimal("0")
    profit_rate = profit_rate or Decimal("0")
    tax_rate = tax_rate or Decimal("0")
    loss_fee = (priced_items_subtotal * loss_rate / 100).quantize(MONEY, ROUND_HALF_UP)
    cost_base = (
        priced_items_subtotal + loss_fee + transport_fee + installation_fee + rush_fee
    ).quantize(MONEY, ROUND_HALF_UP)
    profit_fee = (cost_base * profit_rate / 100).quantize(MONEY, ROUND_HALF_UP)
    pre_tax_total = (cost_base + profit_fee).quantize(MONEY, ROUND_HALF_UP)
    tax_fee = (pre_tax_total * tax_rate / 100).quantize(MONEY, ROUND_HALF_UP)
    grand_total = (pre_tax_total + tax_fee).quantize(MONEY, ROUND_HALF_UP)

    normalized = {
        "project_name": project_name,
        "client_name": str(task_data.get("client_name") or "").strip()[:80],
        "delivery_date": str(task_data.get("delivery_date") or "").strip()[:20],
        "validity_days": str(task_data.get("validity_days") or "15").strip()[:3]
        or "15",
        "quotation_items": json.dumps(
            normalized_items, ensure_ascii=False, separators=(",", ":")
        ),
        "transport_fee": str(transport_fee.quantize(MONEY)),
        "installation_fee": str(installation_fee.quantize(MONEY)),
        "rush_fee": str(rush_fee.quantize(MONEY)),
        "loss_rate": str(loss_rate.normalize()),
        "loss_fee": str(loss_fee),
        "profit_rate": str(profit_rate.normalize()),
        "profit_fee": str(profit_fee),
        "tax_rate": str(tax_rate.normalize()),
        "tax_fee": str(tax_fee),
        "priced_items_subtotal": str(priced_items_subtotal.quantize(MONEY)),
        "cost_base": str(cost_base),
        "pre_tax_total": str(pre_tax_total),
        "grand_total": str(grand_total),
        "pending_inquiry_count": str(pending_count),
        "quotation_status": "估价方案",
        "quotation_notes": str(task_data.get("quotation_notes") or "").strip()[:800],
        "model_choice": "auto",
    }
    return {key: value for key, value in normalized.items() if value != ""}


__all__ = ["normalize_material_quotation", "search_material_prices"]
