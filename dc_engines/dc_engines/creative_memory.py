"""Obsidian-backed company evidence retrieval for creative prompt compilation."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from nas_sync.dc_memory_indexer import (
    dedupe_query_rows,
    fetch_fts_rows,
    fetch_like_rows,
    query_terms,
)

CREATIVE_DOCUMENT_TYPES = {
    "文案素材",
    "执行方案",
    "传播策略",
    "SOP",
    "复盘结算",
    "资料文档",
}
CONFIRMED_REVIEW_STATUSES = {"confirmed", "rule_confirmed"}


def search_company_creative_memory(
    query: str,
    *,
    db_path: str | Path,
    limit: int = 3,
) -> list[dict[str, str]]:
    """Search company originals in the Obsidian-backed NAS memory index.

    Args:
        query: Colleague's current creative brief.
        db_path: Path to the NAS memory SQLite database.
        limit: Maximum number of distinct company sources to return.

    Returns:
        Ranked evidence with title, source, excerpt, review state, and a usage
        boundary suitable for prompt compilation.

    Raises:
        ValueError: If the query is empty, too long, or the limit is invalid.
        sqlite3.Error: If an existing index cannot be queried.
    """
    normalized_query = re.sub(r"\s+", " ", str(query or "")).strip()
    if not normalized_query or len(normalized_query) > 4000:
        raise ValueError("创作记忆检索需要 1-4000 字的任务描述")
    if limit < 1:
        raise ValueError("创作记忆检索数量必须大于 0")
    database = Path(db_path)
    if not database.is_file():
        return []

    terms = query_terms(normalized_query)
    specific_terms = [
        term.casefold() for term in terms if 3 <= len(re.sub(r"\s+", "", term)) <= 12
    ]
    fetch_limit = max(min(limit, 10) * 80, 240)
    with sqlite3.connect(database) as conn:
        conn.row_factory = sqlite3.Row
        rows = fetch_fts_rows(conn, normalized_query, fetch_limit)
        rows.extend(fetch_like_rows(conn, terms, fetch_limit))
    ranked = dedupe_query_rows(
        rows,
        terms,
        normalized_query,
        max(min(limit, 10) * 8, 24),
    )

    evidence: list[dict[str, str]] = []
    for document in ranked:
        doc_type = str(document.get("doc_type") or "").strip()
        if doc_type not in CREATIVE_DOCUMENT_TYPES:
            continue
        if float(document.get("score") or 0) < 12:
            continue
        excerpt = re.sub(
            r"\s+",
            " ",
            str(document.get("text") or document.get("summary") or ""),
        ).strip()
        if not excerpt:
            continue
        searchable = " ".join(
            (
                str(document.get("title") or ""),
                str(document.get("summary") or ""),
                excerpt,
            )
        ).casefold()
        if specific_terms and not any(term in searchable for term in specific_terms):
            continue
        if (
            doc_type == "资料文档"
            and sum(
                marker in excerpt
                for marker in ("单价", "不含税总价", "材质工艺", "报价合计", "供应商")
            )
            >= 3
        ):
            continue
        review_status = str(document.get("review_status") or "need_review").strip()
        confirmed = review_status in CONFIRMED_REVIEW_STATUSES
        evidence.append(
            {
                "title": str(document.get("title") or "公司历史资料").strip()[:120],
                "source_path": str(
                    document.get("rel_path") or document.get("source_path") or ""
                ).strip()[:400],
                "doc_type": doc_type[:40],
                "excerpt": excerpt[:1000],
                "review_status": review_status[:40],
                "source_status": "已复核" if confirmed else "待复核",
                "usage_policy": (
                    "facts_and_style" if confirmed else "style_reference_only"
                ),
            }
        )
        if len(evidence) >= min(limit, 10):
            break
    return evidence


__all__ = ["search_company_creative_memory"]
