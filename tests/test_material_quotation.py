from __future__ import annotations

import json
import sqlite3

import pytest
from dc_engines.material_quotation import (
    normalize_material_quotation,
    search_material_prices,
)


def _quotation_db(tmp_path) -> str:
    db_path = tmp_path / "nas_memory.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE documents (
                doc_key TEXT PRIMARY KEY,
                rel_path TEXT NOT NULL,
                source_path TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                file_size INTEGER NOT NULL,
                parser TEXT NOT NULL,
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                tags_json TEXT NOT NULL,
                indexed_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                archive_path TEXT DEFAULT '',
                project_id TEXT DEFAULT '',
                project_name TEXT DEFAULT '',
                doc_type TEXT DEFAULT '',
                initiator TEXT DEFAULT '',
                owner TEXT DEFAULT '',
                departments_json TEXT DEFAULT '[]',
                participants_json TEXT DEFAULT '[]',
                confidence REAL DEFAULT 0,
                review_status TEXT DEFAULT 'need_review'
            );
            CREATE TABLE chunks (
                chunk_id TEXT PRIMARY KEY,
                doc_key TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                text TEXT NOT NULL
            );
            """
        )
        conn.execute(
            """
            INSERT INTO documents (
                doc_key, rel_path, source_path, sha256, file_size, parser, title,
                summary, tags_json, indexed_at, metadata_json, project_name,
                doc_type, review_status
            ) VALUES (?, ?, ?, '', 1, 'md', ?, ?, '[]', '2026-07-13', '{}', ?, ?, ?)
            """,
            (
                "jumei",
                "供应商库/搭建类/聚美广告.md",
                "/vault/供应商库/搭建类/聚美广告.md",
                "聚美广告",
                "筹备组写真与 UV 历史报价",
                "聚美广告",
                "预算报价",
                "need_review",
            ),
        )
        conn.execute(
            """
            INSERT INTO chunks VALUES (?, ?, 0, ?)
            """,
            (
                "chunk-1",
                "jumei",
                """## UV 彩白彩
| 材料 | 单价（元/㎡） | 备注 |
| --- | ---: | --- |
| UV 超透贴 | 35 | 透明率 99% |
| UV 3M 布 | 120 | - |

## 户内写真
| 项目 | 规格/说明 | 单价（元） |
| --- | --- | ---: |
| 背胶 | 过哑膜 | 8 |
""",
            ),
        )
    return str(db_path)


def test_search_material_prices_returns_structured_obsidian_evidence(tmp_path) -> None:
    records = search_material_prices(
        "UV 超透贴",
        db_path=_quotation_db(tmp_path),
        limit=5,
    )

    assert records[0] == {
        "item_name": "UV 超透贴",
        "specification": "透明率 99%",
        "unit": "㎡",
        "unit_price": "35.00",
        "supplier": "聚美广告",
        "source_path": "供应商库/搭建类/聚美广告.md",
        "source_status": "待复核",
    }


def test_search_material_prices_keeps_missing_unit_empty(tmp_path) -> None:
    records = search_material_prices(
        "背胶",
        db_path=_quotation_db(tmp_path),
        limit=5,
    )

    assert records[0]["unit"] == ""
    assert records[0]["unit_price"] == "8.00"


def test_normalize_material_quotation_recalculates_every_charge() -> None:
    normalized = normalize_material_quotation(
        {
            "project_name": "柳州用户共创会",
            "client_name": "五菱",
            "quotation_items": json.dumps(
                [
                    {
                        "item_name": "背胶",
                        "specification": "过哑膜",
                        "remark": "含安装，正式采购前复核",
                        "image_paths": [
                            "projects/筹备组物料报价/draft/images/背胶正面.png",
                            "projects/筹备组物料报价/draft/images/背胶侧面.png",
                        ],
                        "quantity": "10",
                        "unit": "㎡",
                        "unit_price": "8",
                        "supplier": "柳州东成广告",
                        "source_path": "供应商库/柳州-南宁搭建物料.md",
                    },
                    {
                        "item_name": "现场摄影",
                        "quantity": "1",
                        "unit": "项",
                        "unit_price": "",
                    },
                ],
                ensure_ascii=False,
            ),
            "loss_rate": "5",
            "transport_fee": "100",
            "installation_fee": "200",
            "rush_fee": "50",
            "profit_rate": "10",
            "tax_rate": "6",
            "validity_days": "15",
        }
    )
    items = json.loads(normalized["quotation_items"])

    assert items[0]["line_subtotal"] == "80.00"
    assert items[0]["remark"] == "含安装，正式采购前复核"
    assert items[0]["image_path"] == (
        "projects/筹备组物料报价/draft/images/背胶正面.png"
    )
    assert items[0]["image_paths"] == [
        "projects/筹备组物料报价/draft/images/背胶正面.png",
        "projects/筹备组物料报价/draft/images/背胶侧面.png",
    ]
    assert items[1]["price_status"] == "待询价"
    assert normalized["priced_items_subtotal"] == "80.00"
    assert normalized["loss_fee"] == "4.00"
    assert normalized["cost_base"] == "434.00"
    assert normalized["profit_fee"] == "43.40"
    assert normalized["pre_tax_total"] == "477.40"
    assert normalized["tax_fee"] == "28.64"
    assert normalized["grand_total"] == "506.04"
    assert normalized["pending_inquiry_count"] == "1"
    assert normalized["quotation_status"] == "估价方案"


def test_normalize_material_quotation_rejects_invalid_rows() -> None:
    with pytest.raises(ValueError, match="数量"):
        normalize_material_quotation(
            {
                "project_name": "测试报价",
                "quotation_items": json.dumps(
                    [{"item_name": "背胶", "quantity": "0", "unit": "㎡"}]
                ),
            }
        )

    with pytest.raises(ValueError, match="单位"):
        normalize_material_quotation(
            {
                "project_name": "测试报价",
                "quotation_items": json.dumps(
                    [
                        {
                            "item_name": "背胶",
                            "quantity": "10",
                            "unit": "",
                            "unit_price": "8",
                        }
                    ]
                ),
            }
        )

    with pytest.raises(ValueError, match="最多上传 6 张"):
        normalize_material_quotation(
            {
                "project_name": "测试报价",
                "quotation_items": json.dumps(
                    [
                        {
                            "item_name": "礼盒",
                            "quantity": "1",
                            "unit": "套",
                            "unit_price": "10",
                            "image_paths": [
                                f"projects/筹备组物料报价/draft/images/{index}.png"
                                for index in range(7)
                            ],
                        }
                    ],
                    ensure_ascii=False,
                ),
            }
        )
