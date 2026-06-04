from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ContentSopSourceContext:
    knowledge_context: str
    source_citations: list[dict[str, str]]


def assemble_content_sop_source_context(
    memory_context: dict[str, Any] | None,
    *,
    max_chars: int = 1800,
) -> ContentSopSourceContext:
    """Convert NAS/Obsidian memory hits into content-SOP context fields."""
    if not memory_context:
        return ContentSopSourceContext(knowledge_context="", source_citations=[])

    lines: list[str] = []
    citations: list[dict[str, str]] = []
    project_items = memory_context.get("project_items") or []
    documents = memory_context.get("documents") or []

    if project_items:
        lines.append("项目关系上下文:")
        for item in project_items[:5]:
            if not isinstance(item, dict):
                continue
            source_path = str(item.get("source_rel_path") or "")
            project_name = str(item.get("project_name") or "")
            owner = str(item.get("owner") or "未标注")
            owner_department = str(item.get("owner_department") or "未标注")
            lines.append(
                f"- 项目={project_name or '未标注'}；负责人={owner}；"
                f"部门={owner_department}；来源={source_path}"
            )
            if source_path:
                citations.append(
                    {
                        "type": "project_item",
                        "title": project_name,
                        "source_path": source_path,
                        "owner": owner,
                        "department": owner_department,
                    }
                )

    if documents:
        lines.append("相关文档上下文:")
        for doc in documents[:5]:
            if not isinstance(doc, dict):
                continue
            title = str(doc.get("title") or "")
            source_path = str(doc.get("rel_path") or doc.get("source_path") or "")
            project_name = str(doc.get("project_name") or "")
            owner = str(doc.get("owner") or "未标注")
            review_status = str(doc.get("review_status") or "")
            departments = _json_list(doc.get("departments_json"))
            summary = _compact(str(doc.get("summary") or doc.get("text") or ""))[:280]
            lines.append(
                f"- 文档={title or '未标注'}；项目={project_name or '未标注'}；"
                f"负责人={owner}；部门={','.join(departments) or '未标注'}；"
                f"状态={review_status or 'unknown'}；来源={source_path}；摘要={summary}"
            )
            if source_path:
                citations.append(
                    {
                        "type": "document",
                        "title": title,
                        "source_path": source_path,
                        "project_name": project_name,
                        "owner": owner,
                        "review_status": review_status,
                    }
                )

    context = "\n".join(lines).strip()
    if len(context) > max_chars:
        context = context[: max_chars - 20].rstrip() + "\n...已截断"
    return ContentSopSourceContext(
        knowledge_context=context,
        source_citations=_dedupe_citations(citations),
    )


def strip_internal_memory_context(text: str) -> str:
    return re.sub(
        r"\s*<dc_agent_memory_context>.*?</dc_agent_memory_context>\s*",
        "\n",
        text,
        flags=re.S,
    ).strip()


def _json_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if not isinstance(value, str):
        return []
    try:
        parsed = json.loads(value or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if str(item).strip()]


def _compact(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _dedupe_citations(citations: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    deduped: list[dict[str, str]] = []
    for citation in citations:
        key = citation.get("source_path") or citation.get("title") or ""
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(citation)
    return deduped
