"""W3 飞书资料查询引擎。

两个版本：
- ``query_resources_v0`` — 只匹配白名单元信息（name/description）。无 client 时用
- ``query_resources_v1`` — 真飞书 API 拉文档/表格内容，关键词在内容里命中评分

Plugin 自动决定走哪个：FeishuClient 存在 + enabled → v1；否则 v0。

v1 简单评分：
- 文档：keyword 在 plain_text 出现 N 次 → score = min(N * 0.1, 1.0) + domain 加成
- 表格：keyword 在任一 record 字段命中 → score = min(命中记录数 * 0.1, 1.0) + domain 加成

不做 embedding（留下一版 R2 嵌入分类器一并接）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from typing import TYPE_CHECKING, Any

from .contracts import QueryHit, Whitelist

if TYPE_CHECKING:
    from .client import FeishuClient

logger = logging.getLogger(__name__)

_SNIPPET_RADIUS = 40  # 命中位置前后多少字符做 snippet
_DEFAULT_CONTENT_MAX_SOURCES = 10
_DEFAULT_TABLE_RECORD_LIMIT = 500
_DEFAULT_CONTENT_TIMEOUT_SECONDS = 10.0


def _make_metadata(
    *,
    source_type: str,
    retrieval_mode: str,
    credential_status: str,
    **extra: str,
) -> dict[str, str]:
    metadata = {
        "source_type": source_type,
        "retrieval_mode": retrieval_mode,
        "credential_status": credential_status,
    }
    metadata.update({key: value for key, value in extra.items() if value})
    return metadata


def _score_meta(text: str, keyword: str) -> float:
    if not text or not keyword:
        return 0.0
    t = text.lower()
    k = keyword.lower()
    if k in t:
        return 1.0
    parts = [p for p in k.split() if p]
    if parts and all(p in t for p in parts):
        return 0.7
    if any(p in t for p in parts):
        return 0.4
    return 0.0


def _score_meta_with_domain(
    text: str,
    keyword: str,
    *,
    domain: str,
    domain_hint: str | None,
) -> float:
    score = _score_meta(text, keyword)
    if domain_hint and domain == domain_hint:
        score += 0.3
    return score


def _resolve_positive_int(value: int | None, default: int) -> int:
    if value is None:
        return default
    return max(0, value)


async def _with_optional_timeout(
    awaitable: Awaitable[Any],
    timeout_seconds: float | None,
) -> Any:
    if timeout_seconds is None or timeout_seconds <= 0:
        return await awaitable
    return await asyncio.wait_for(awaitable, timeout=timeout_seconds)


def _make_snippet(text: str, keyword: str) -> str:
    if not text or not keyword:
        return ""
    idx = text.lower().find(keyword.lower())
    if idx < 0:
        return text[: 2 * _SNIPPET_RADIUS].replace("\n", " ")
    start = max(0, idx - _SNIPPET_RADIUS)
    end = min(len(text), idx + len(keyword) + _SNIPPET_RADIUS)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{text[start:end].strip()}{suffix}".replace("\n", " ")


def query_resources_v0(
    keyword: str,
    *,
    whitelist: Whitelist,
    domain_hint: str | None = None,
    limit: int = 5,
    credential_status: str = "missing",
) -> list[QueryHit]:
    """v0 同步元信息检索（无 FeishuClient 时使用）。"""
    if not keyword or not whitelist:
        return []
    keyword = keyword.strip()
    if not keyword:
        return []
    candidates: list[QueryHit] = []

    for d in whitelist.documents:
        s = _score_meta(f"{d.name} {d.description}", keyword)
        if domain_hint and d.domain == domain_hint:
            s += 0.3
        if s > 0:
            candidates.append(
                QueryHit(
                    source_type="document",
                    source_id=d.doc_token,
                    title=d.name,
                    domain=d.domain,
                    summary=d.description,
                    matched_field="meta",
                    score=s,
                    url=f"https://feishu.cn/docx/{d.doc_token}",
                    metadata=_make_metadata(
                        source_type="document",
                        retrieval_mode="metadata_only",
                        credential_status=credential_status,
                        content_status="not_requested",
                    ),
                )
            )

    for t in whitelist.tables:
        s = _score_meta(f"{t.name} {t.description}", keyword)
        if domain_hint and t.domain == domain_hint:
            s += 0.3
        if s > 0:
            candidates.append(
                QueryHit(
                    source_type="table",
                    source_id=t.table_id,
                    title=t.name,
                    domain=t.domain,
                    summary=t.description,
                    matched_field="meta",
                    score=s,
                    url=f"https://feishu.cn/base/{t.app_token}?table={t.table_id}"
                    if t.app_token
                    else None,
                    metadata=_make_metadata(
                        source_type="table",
                        retrieval_mode="metadata_only",
                        credential_status=credential_status,
                        content_status="not_requested",
                    ),
                )
            )

    for f in whitelist.folders:
        s = _score_meta(f"{f.name} {f.description}", keyword)
        if domain_hint and f.domain == domain_hint:
            s += 0.3
        if s > 0:
            candidates.append(
                QueryHit(
                    source_type="folder",
                    source_id=f.kb_id,
                    title=f.name,
                    domain=f.domain,
                    summary=f.description,
                    matched_field="meta",
                    score=s,
                    metadata=_make_metadata(
                        source_type="folder",
                        retrieval_mode="metadata_only",
                        credential_status=credential_status,
                        content_status="not_requested",
                    ),
                )
            )

    candidates.sort(key=lambda h: h.score, reverse=True)
    return candidates[:limit]


async def query_resources_v1(
    keyword: str,
    *,
    whitelist: Whitelist,
    client: FeishuClient,
    domain_hint: str | None = None,
    limit: int = 5,
    max_sources: int | None = _DEFAULT_CONTENT_MAX_SOURCES,
    table_record_limit: int = _DEFAULT_TABLE_RECORD_LIMIT,
    content_timeout_seconds: float | None = _DEFAULT_CONTENT_TIMEOUT_SECONDS,
) -> list[QueryHit]:
    """v1 真内容检索：拉文档/表格内容，在内容里找 keyword。

    先做元信息预筛，再只并发拉取有界数量的内容源。每个失败的源退回元信息。
    """
    if not keyword or not whitelist:
        return []
    keyword = keyword.strip()
    if not keyword:
        return []

    credential_status = "enabled" if client.enabled else "disabled"
    if not client.enabled:
        return query_resources_v0(
            keyword,
            whitelist=whitelist,
            domain_hint=domain_hint,
            limit=limit,
            credential_status=credential_status,
        )

    doc_scores: dict[str, float] = {}
    table_scores: dict[tuple[str, str], float] = {}
    content_candidates: list[tuple[float, int, str, object]] = []

    source_order = 0
    for d in whitelist.documents:
        score = _score_meta_with_domain(
            f"{d.name} {d.description}",
            keyword,
            domain=d.domain,
            domain_hint=domain_hint,
        )
        doc_scores[d.doc_token] = score
        content_candidates.append((score, source_order, "document", d))
        source_order += 1

    for t in whitelist.tables:
        score = _score_meta_with_domain(
            f"{t.name} {t.description}",
            keyword,
            domain=t.domain,
            domain_hint=domain_hint,
        )
        table_key = (t.app_token, t.table_id)
        table_scores[table_key] = score
        if t.app_token:
            content_candidates.append((score, source_order, "table", t))
            source_order += 1

    content_source_limit = _resolve_positive_int(
        max_sources, _DEFAULT_CONTENT_MAX_SOURCES
    )
    selected_sources = sorted(
        content_candidates,
        key=lambda candidate: (-candidate[0], candidate[1]),
    )[:content_source_limit]
    selected_docs = [
        source
        for _score, _order, source_type, source in selected_sources
        if source_type == "document"
    ]
    selected_tables = [
        source
        for _score, _order, source_type, source in selected_sources
        if source_type == "table"
    ]
    selected_doc_tokens = {d.doc_token for d in selected_docs}
    selected_table_keys = {(t.app_token, t.table_id) for t in selected_tables}

    record_limit = max(1, table_record_limit)
    doc_tasks = [
        _with_optional_timeout(
            client.read_document(d.doc_token),
            content_timeout_seconds,
        )
        for d in selected_docs
    ]
    table_tasks = [
        _with_optional_timeout(
            client.read_table_records(t.app_token, t.table_id, limit=record_limit),
            content_timeout_seconds,
        )
        for t in selected_tables
    ]

    doc_results, table_results = await asyncio.gather(
        asyncio.gather(*doc_tasks, return_exceptions=True)
        if doc_tasks
        else _empty_gather(),
        asyncio.gather(*table_tasks, return_exceptions=True)
        if table_tasks
        else _empty_gather(),
    )

    candidates: list[QueryHit] = []

    # 文档评分
    for d, content in zip(selected_docs, doc_results):
        content_error = content if isinstance(content, Exception) else None
        if content_error is not None or content is None:
            s = doc_scores[d.doc_token]
            if s > 0:
                content_status = "error" if content_error is not None else "empty"
                candidates.append(
                    QueryHit(
                        source_type="document",
                        source_id=d.doc_token,
                        title=d.name,
                        domain=d.domain,
                        summary=d.description,
                        matched_field="meta",
                        matched_snippet="",
                        score=s,
                        url=f"https://feishu.cn/docx/{d.doc_token}",
                        metadata=_make_metadata(
                            source_type="document",
                            retrieval_mode="metadata_only",
                            credential_status=credential_status,
                            content_status=content_status,
                            error_type=type(content_error).__name__
                            if content_error is not None
                            else "",
                        ),
                    )
                )
            continue
        full_text = f"{content.title}\n{content.plain_text}"
        if not full_text.strip():
            s = doc_scores[d.doc_token]
            if s > 0:
                candidates.append(
                    QueryHit(
                        source_type="document",
                        source_id=d.doc_token,
                        title=d.name,
                        domain=d.domain,
                        summary=d.description,
                        matched_field="meta",
                        matched_snippet="",
                        score=s,
                        url=f"https://feishu.cn/docx/{d.doc_token}",
                        metadata=_make_metadata(
                            source_type="document",
                            retrieval_mode="metadata_only",
                            credential_status=credential_status,
                            content_status="empty",
                        ),
                    )
                )
            continue
        cnt = full_text.lower().count(keyword.lower())
        if cnt == 0:
            # 退到 meta 匹配
            s = doc_scores[d.doc_token]
        else:
            s = min(0.5 + cnt * 0.1, 1.0)
        if domain_hint and d.domain == domain_hint:
            s += 0.3
        if s > 0:
            candidates.append(
                QueryHit(
                    source_type="document",
                    source_id=d.doc_token,
                    title=content.title or d.name,
                    domain=d.domain,
                    summary=d.description,
                    matched_field="content" if cnt > 0 else "meta",
                    matched_snippet=_make_snippet(full_text, keyword)
                    if cnt > 0
                    else "",
                    score=s,
                    url=f"https://feishu.cn/docx/{d.doc_token}",
                    metadata=_make_metadata(
                        source_type="document",
                        retrieval_mode="content" if cnt > 0 else "metadata_only",
                        credential_status=credential_status,
                        content_status="matched" if cnt > 0 else "searched_no_match",
                    ),
                )
            )

    # 表格评分
    for t, records in zip(selected_tables, table_results):
        records_error = records if isinstance(records, Exception) else None
        if records_error is not None or records is None or not records:
            table_key = (t.app_token, t.table_id)
            s = table_scores[table_key]
            if s > 0:
                if records_error is not None:
                    content_status = "error"
                elif records is None:
                    content_status = "unavailable"
                else:
                    content_status = "empty_records"
                candidates.append(
                    QueryHit(
                        source_type="table",
                        source_id=t.table_id,
                        title=t.name,
                        domain=t.domain,
                        summary=t.description,
                        matched_field="meta",
                        matched_snippet="",
                        score=s,
                        url=f"https://feishu.cn/base/{t.app_token}?table={t.table_id}"
                        if t.app_token
                        else None,
                        metadata=_make_metadata(
                            source_type="table",
                            retrieval_mode="metadata_only",
                            credential_status=credential_status,
                            content_status=content_status,
                            error_type=type(records_error).__name__
                            if records_error is not None
                            else "",
                        ),
                    )
                )
            continue
        matched_records = 0
        sample_snippet = ""
        for r in records:
            joined = " ".join(str(v) for v in r.fields.values())
            if keyword.lower() in joined.lower():
                matched_records += 1
                if not sample_snippet:
                    sample_snippet = _make_snippet(joined, keyword)
        if matched_records == 0:
            s = table_scores[(t.app_token, t.table_id)]
            sample_snippet = ""
        else:
            s = min(0.5 + matched_records * 0.1, 1.0)
        if domain_hint and t.domain == domain_hint:
            s += 0.3
        if s > 0:
            candidates.append(
                QueryHit(
                    source_type="table",
                    source_id=t.table_id,
                    title=t.name,
                    domain=t.domain,
                    summary=(
                        f"{t.description} · {len(records)} 条记录、命中 {matched_records}"
                        if matched_records > 0
                        else t.description
                    ),
                    matched_field="content" if matched_records > 0 else "meta",
                    matched_snippet=sample_snippet,
                    score=s,
                    url=f"https://feishu.cn/base/{t.app_token}?table={t.table_id}",
                    metadata=_make_metadata(
                        source_type="table",
                        retrieval_mode="content"
                        if matched_records > 0
                        else "metadata_only",
                        credential_status=credential_status,
                        content_status="matched"
                        if matched_records > 0
                        else "searched_no_match",
                    ),
                )
            )

    # 未拉内容的元信息命中仍可返回，但 provenance 明确为 not_requested。
    for d in whitelist.documents:
        if d.doc_token in selected_doc_tokens:
            continue
        s = doc_scores[d.doc_token]
        if s > 0:
            candidates.append(
                QueryHit(
                    source_type="document",
                    source_id=d.doc_token,
                    title=d.name,
                    domain=d.domain,
                    summary=d.description,
                    matched_field="meta",
                    score=s,
                    url=f"https://feishu.cn/docx/{d.doc_token}",
                    metadata=_make_metadata(
                        source_type="document",
                        retrieval_mode="metadata_only",
                        credential_status=credential_status,
                        content_status="not_requested",
                    ),
                )
            )

    for t in whitelist.tables:
        table_key = (t.app_token, t.table_id)
        if table_key in selected_table_keys:
            continue
        s = table_scores[table_key]
        if s > 0:
            candidates.append(
                QueryHit(
                    source_type="table",
                    source_id=t.table_id,
                    title=t.name,
                    domain=t.domain,
                    summary=t.description,
                    matched_field="meta",
                    score=s,
                    url=f"https://feishu.cn/base/{t.app_token}?table={t.table_id}"
                    if t.app_token
                    else None,
                    metadata=_make_metadata(
                        source_type="table",
                        retrieval_mode="metadata_only",
                        credential_status=credential_status,
                        content_status="not_requested"
                        if t.app_token
                        else "missing_app_token",
                    ),
                )
            )

    # KB 文件夹：v1 暂无客户端集成，仍走元信息（沿用 v0 算法）
    for f in whitelist.folders:
        s = _score_meta_with_domain(
            f"{f.name} {f.description}",
            keyword,
            domain=f.domain,
            domain_hint=domain_hint,
        )
        if s > 0:
            candidates.append(
                QueryHit(
                    source_type="folder",
                    source_id=f.kb_id,
                    title=f.name,
                    domain=f.domain,
                    summary=f.description,
                    matched_field="meta",
                    score=s,
                    metadata=_make_metadata(
                        source_type="folder",
                        retrieval_mode="metadata_only",
                        credential_status=credential_status,
                        content_status="unsupported",
                    ),
                )
            )

    candidates.sort(key=lambda h: h.score, reverse=True)
    return candidates[:limit]


# 向后兼容：保留 query_resources 别名，默认行为 = v0
def query_resources(
    keyword: str,
    *,
    whitelist: Whitelist,
    domain_hint: str | None = None,
    limit: int = 5,
) -> list[QueryHit]:
    return query_resources_v0(
        keyword, whitelist=whitelist, domain_hint=domain_hint, limit=limit
    )


async def _empty_gather():
    return []
