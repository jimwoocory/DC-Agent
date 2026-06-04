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
from typing import TYPE_CHECKING

from .contracts import QueryHit, Whitelist

if TYPE_CHECKING:
    from .client import FeishuClient

logger = logging.getLogger(__name__)

_SNIPPET_RADIUS = 40  # 命中位置前后多少字符做 snippet


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
) -> list[QueryHit]:
    """v0 同步元信息检索（无 FeishuClient 时使用）。"""
    if not keyword or not whitelist:
        return []
    keyword = keyword.strip()
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
) -> list[QueryHit]:
    """v1 真内容检索：拉文档/表格内容，在内容里找 keyword。

    并发拉所有白名单源（防卡顿）。每个失败的源跳过。
    """
    if not keyword or not whitelist or not client.enabled:
        return query_resources_v0(
            keyword, whitelist=whitelist, domain_hint=domain_hint, limit=limit
        )
    keyword = keyword.strip()

    # 并发拉所有源
    doc_tasks = [client.read_document(d.doc_token) for d in whitelist.documents]
    table_tasks = [
        client.read_table_records(t.app_token, t.table_id, limit=500)
        for t in whitelist.tables
        if t.app_token  # 缺 app_token 跳过
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
    for d, content in zip(whitelist.documents, doc_results):
        if isinstance(content, Exception) or content is None:
            continue
        full_text = f"{content.title}\n{content.plain_text}"
        cnt = full_text.lower().count(keyword.lower())
        if cnt == 0:
            # 退到 meta 匹配
            s = _score_meta(f"{d.name} {d.description}", keyword)
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
                )
            )

    # 表格评分
    tables_with_token = [t for t in whitelist.tables if t.app_token]
    for t, records in zip(tables_with_token, table_results):
        if isinstance(records, Exception) or not records:
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
            s = _score_meta(f"{t.name} {t.description}", keyword)
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
                )
            )

    # KB 文件夹：v1 暂无客户端集成，仍走元信息（沿用 v0 算法）
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
