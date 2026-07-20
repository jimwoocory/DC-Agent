from __future__ import annotations

import json
import sqlite3
from io import BytesIO

from dc_engines.material_quotation import search_material_prices
from dc_engines.supplier_price_intake import (
    parse_supplier_price_attachment,
    save_supplier_price_submission,
)
from openpyxl import Workbook


def test_parse_supplier_price_attachment_reads_xlsx_rows() -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["物料", "规格", "单位", "单价（元）"])
    sheet.append(["X 展架", "0.8×1.8m", "个", 32])
    stream = BytesIO()
    workbook.save(stream)

    rows = parse_supplier_price_attachment("合作公司报价.xlsx", stream.getvalue())

    assert rows == [
        {
            "item_name": "X 展架",
            "specification": "0.8×1.8m",
            "unit": "个",
            "unit_price": "32.00",
        }
    ]


def test_save_supplier_price_submission_archives_and_indexes_manual_rows(
    tmp_path,
) -> None:
    vault_path = tmp_path / "ObsidianVault"
    nas_path = tmp_path / "nas"
    nas_path.mkdir()
    db_path = tmp_path / "nas_memory.db"

    result = save_supplier_price_submission(
        {
            "supplier_name": "柳州新伙伴广告有限公司",
            "category": "搭建类",
            "quote_date": "2026-07-15",
            "notes": "含税，不含运输",
            "items": [
                {
                    "item_name": "背胶",
                    "specification": "过哑膜",
                    "unit": "㎡",
                    "unit_price": "8.5",
                },
                {
                    "item_name": "X 展架",
                    "specification": "0.8×1.8m",
                    "unit": "个",
                    "unit_price": "32",
                },
            ],
        },
        submitted_by="测试员工",
        vault_path=vault_path,
        nas_path=nas_path,
        db_path=db_path,
        attachment_name="原始报价.pdf",
        attachment_bytes=b"%PDF-1.4 employee source",
    )

    note_path = vault_path / result["relative_path"]
    assert note_path.is_file()
    assert "员工录入" in note_path.read_text(encoding="utf-8")
    assert "柳州新伙伴广告有限公司" in note_path.read_text(encoding="utf-8")
    nas_archive = nas_path / result["nas_archive_path"]
    assert (nas_archive / "报价记录.md").is_file()
    assert (nas_archive / result["attachment_filename"]).is_file()
    assert result["item_count"] == 2
    assert result["review_status"] == "待复核"

    matches = search_material_prices("背胶", db_path=db_path)
    assert matches[0]["supplier"] == "柳州新伙伴广告有限公司"
    assert matches[0]["unit"] == "㎡"
    assert matches[0]["unit_price"] == "8.50"
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT review_status, metadata_json FROM documents"
        ).fetchone()
    assert row[0] == "need_review"
    assert json.loads(row[1])["source"] == "employee_supplier_price_intake"


def test_save_supplier_price_submission_requires_a_real_unit(tmp_path) -> None:
    nas_path = tmp_path / "nas"
    nas_path.mkdir()
    try:
        save_supplier_price_submission(
            {
                "supplier_name": "测试供应商",
                "items": [
                    {
                        "item_name": "背胶",
                        "unit": "",
                        "unit_price": "8",
                    }
                ],
            },
            submitted_by="测试员工",
            vault_path=tmp_path / "vault",
            nas_path=nas_path,
            db_path=tmp_path / "nas_memory.db",
        )
    except ValueError as error:
        assert "单位" in str(error)
    else:
        raise AssertionError("missing unit must be rejected")
