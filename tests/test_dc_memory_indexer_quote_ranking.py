import pytest

from nas_sync.dc_memory_indexer import dedupe_query_rows, load_config, query_terms


def test_quote_query_ranks_budget_and_unit_price_evidence_first() -> None:
    query = "做一个柳州活动报价"
    rows = [
        {
            "doc_key": "execution",
            "title": "柳州活动执行方案",
            "project_name": "柳州活动",
            "doc_type": "执行方案",
            "parser": "md",
            "summary": "柳州活动执行流程",
            "text": "签到、流程、撤场",
            "chunk_index": 0,
            "match_source": "like",
        },
        {
            "doc_key": "quotation",
            "title": "柳州搭建物料报价",
            "project_name": "柳州搭建物料",
            "doc_type": "预算报价",
            "parser": "md",
            "summary": "柳州历史报价",
            "text": "背胶单价 8 元/㎡，写真布单价 18 元/㎡",
            "chunk_index": 0,
            "match_source": "like",
        },
    ]

    result = dedupe_query_rows(rows, query_terms(query), query, 2)

    assert [row["doc_key"] for row in result] == ["quotation", "execution"]


def test_load_config_allows_nas_mount_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DC_NAS_MOUNT_POINT", "/mnt/nas")

    config = load_config()

    assert config["nas"]["mount_point"] == "/mnt/nas"
