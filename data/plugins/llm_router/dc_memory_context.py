"""Inject DC-Agent NAS memory context into business assistant turns."""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

DC_ROOT = Path("/Users/dianchi/DC-Agent")
NAS_MEMORY_DB = Path("/Users/dianchi/DC-Agent/data/nas_memory.db")
GOVERNED_MEMORY_DB = Path("/Users/dianchi/DC-Agent/data/governed_memory.db")
BUSINESS_PLATFORM_ID = "巅池-Agent小助手"
SUPPORTED_PLATFORM_IDS = {
    BUSINESS_PLATFORM_ID,
    # Reserved integration points. Keep disabled until each assistant has a
    # reviewed prompt policy and permission boundary for company memory.
    # "巅池-技术（DevOps）",
    # "巅池-推广 01",
}
MEMORY_MARKER = "<dc_agent_memory_context>"

if str(DC_ROOT) not in sys.path:
    sys.path.insert(0, str(DC_ROOT))
DC_ENGINES_ROOT = DC_ROOT / "dc_engines"
if str(DC_ENGINES_ROOT) not in sys.path:
    sys.path.insert(0, str(DC_ENGINES_ROOT))

from dc_engines.memory_governance.recall import list_recall_memories  # noqa: E402
from dc_engines.memory_governance.store import MemoryGovernanceStore  # noqa: E402

from nas_sync.dc_memory_indexer import (  # noqa: E402
    dedupe_query_rows,
    fetch_fts_rows,
    fetch_like_rows,
    query_terms,
)

MEMORY_HINT_RE = re.compile(
    r"(项目|方案|策划|执行|SOP|负责人|谁负责|发起人|部门|同事|员工|文案|脚本|"
    r"预算|报价|排期|复盘|结算|舆情|KOW|KOS|五菱|菱听|鉴宝|归档|资料|文件|"
    r"飞书|链接|推文|素材|星光|缤果|宏光|宝骏|柳汽|东风|之光|之前|历史|记忆|"
    r"查一下|找一下|有没有|是什么|是谁)",
    re.IGNORECASE,
)

STOP_WORDS = {
    "帮我",
    "一下",
    "这个",
    "那个",
    "现在",
    "之前",
    "有没有",
    "是什么",
    "是谁",
    "怎么",
    "能不能",
    "可以",
    "需要",
    "看看",
    "查询",
    "检索",
}


def _clean_text(text: str) -> str:
    text = re.sub(
        r"<dc_agent_memory_context>.*?</dc_agent_memory_context>", "", text, flags=re.S
    )
    return re.sub(r"\s+", " ", text).strip()


def _candidate_terms(text: str) -> list[str]:
    clean = _clean_text(text)
    terms: list[str] = []
    terms.extend(re.findall(r"[A-Za-z0-9_-]{2,}", clean))
    chinese_chunks = re.findall(r"[\u4e00-\u9fff]{2,18}", clean)
    terms.extend(chinese_chunks)
    for chunk in chinese_chunks:
        for marker in (
            "负责",
            "发起",
            "参与",
            "部门",
            "项目",
            "方案",
            "资料",
            "文件",
            "是谁",
            "是什么",
        ):
            if marker in chunk:
                before, after = chunk.split(marker, 1)
                if 2 <= len(before) <= 12:
                    terms.append(before)
                if 2 <= len(after) <= 12:
                    terms.append(after)
        terms.extend(re.findall(r"[\u4e00-\u9fff]{2,4}", chunk))
    filtered: list[str] = []
    seen: set[str] = set()
    for term in terms:
        term = term.strip()
        if not term or term in STOP_WORDS:
            continue
        if len(term) > 18:
            continue
        if term not in seen:
            seen.add(term)
            filtered.append(term)
    return filtered[:8]


def _json_loads(value: str, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except json.JSONDecodeError:
        return fallback


def _should_retrieve(text: str) -> bool:
    clean = _clean_text(text)
    if len(clean) < 2:
        return False
    return bool(MEMORY_HINT_RE.search(clean))


def _like_clause(columns: list[str], terms: list[str]) -> tuple[str, list[str]]:
    clauses: list[str] = []
    args: list[str] = []
    for term in terms:
        sub = []
        for column in columns:
            sub.append(f"{column} LIKE ?")
            args.append(f"%{term}%")
        clauses.append("(" + " OR ".join(sub) + ")")
    return " OR ".join(clauses), args


def retrieve_memory_context(text: str, *, limit: int = 5) -> dict[str, Any]:
    governed_context = retrieve_governed_memory_context(text, limit=limit)
    if governed_context.get("governed_memories"):
        return governed_context

    if not NAS_MEMORY_DB.exists() or not _should_retrieve(text):
        return {"documents": [], "project_items": []}

    terms = query_terms(_clean_text(text))
    project_terms = _candidate_terms(text)
    if not terms and not project_terms:
        return {"documents": [], "project_items": []}

    with sqlite3.connect(NAS_MEMORY_DB) as conn:
        conn.row_factory = sqlite3.Row

        project_items = []
        if project_terms:
            item_where, item_args = _like_clause(
                [
                    "project_name",
                    "project_type",
                    "project_status",
                    "owner",
                    "owner_department",
                ],
                project_terms,
            )
            project_items = conn.execute(
                f"""
                SELECT project_name, project_type, project_status, owner,
                       owner_department, source_rel_path, evidence_json
                FROM project_items
                WHERE {item_where}
                ORDER BY updated_at DESC, project_name
                LIMIT ?
                """,
                [*item_args, limit],
            ).fetchall()

        fetch_limit = max(limit * 80, limit, 1000)
        rows = fetch_fts_rows(conn, _clean_text(text), fetch_limit)
        rows.extend(fetch_like_rows(conn, terms, fetch_limit))
        documents = dedupe_query_rows(rows, terms, _clean_text(text), limit)

    return {
        "terms": terms,
        "project_items": [dict(row) for row in project_items],
        "documents": documents,
    }


def retrieve_governed_memory_context(text: str, *, limit: int = 5) -> dict[str, Any]:
    if not GOVERNED_MEMORY_DB.exists() or not _should_retrieve(text):
        return {"governed_memories": [], "documents": [], "project_items": []}

    store = MemoryGovernanceStore(GOVERNED_MEMORY_DB)
    memories = []
    seen_memory_ids: set[str] = set()
    for query in [_clean_text(text), *_candidate_terms(text)]:
        if not query:
            continue
        for memory in list_recall_memories(store=store, query=query, limit=limit):
            if memory.memory_id in seen_memory_ids:
                continue
            seen_memory_ids.add(memory.memory_id)
            memories.append(memory)
            if len(memories) >= limit:
                break
        if len(memories) >= limit:
            break
    if not memories:
        return {"governed_memories": [], "documents": [], "project_items": []}
    return {
        "governed_memories": [
            {
                "memory_id": memory.memory_id,
                "title": memory.title,
                "canonical_text": memory.canonical_text,
                "summary": memory.summary,
                "source_path": memory.source_path,
                "source_id": memory.source_id,
                "review_status": memory.review_status,
                "sensitivity": memory.sensitivity,
                "confidence": memory.confidence,
                "owner": memory.owner,
                "project_id": memory.project_id,
                "tags": memory.tags,
                "links": memory.links,
            }
            for memory in memories
        ],
        "documents": [],
        "project_items": [],
    }


def retrieve_memory_context_legacy(text: str, *, limit: int = 5) -> dict[str, Any]:
    """Legacy LIKE-only retrieval kept for short-term comparison/debugging."""
    if not NAS_MEMORY_DB.exists() or not _should_retrieve(text):
        return {"documents": [], "project_items": []}

    terms = _candidate_terms(text)
    if not terms:
        return {"documents": [], "project_items": []}

    with sqlite3.connect(NAS_MEMORY_DB) as conn:
        conn.row_factory = sqlite3.Row

        item_where, item_args = _like_clause(
            [
                "project_name",
                "project_type",
                "project_status",
                "owner",
                "owner_department",
            ],
            terms,
        )
        project_items = conn.execute(
            f"""
            SELECT project_name, project_type, project_status, owner,
                   owner_department, source_rel_path, evidence_json
            FROM project_items
            WHERE {item_where}
            ORDER BY updated_at DESC, project_name
            LIMIT ?
            """,
            [*item_args, limit],
        ).fetchall()

        doc_where, doc_args = _like_clause(
            [
                "d.title",
                "d.project_name",
                "d.doc_type",
                "d.owner",
                "d.initiator",
                "d.participants_json",
                "d.summary",
                "c.text",
            ],
            terms,
        )
        documents = conn.execute(
            f"""
            SELECT d.title, d.rel_path, d.project_name, d.doc_type, d.owner,
                   d.initiator, d.departments_json, d.participants_json,
                   d.review_status, d.summary, c.text
            FROM documents d
            JOIN chunks c ON c.doc_key = d.doc_key
            WHERE {doc_where}
            GROUP BY d.doc_key
            ORDER BY d.indexed_at DESC
            LIMIT ?
            """,
            [*doc_args, limit],
        ).fetchall()

    return {
        "terms": terms,
        "project_items": [dict(row) for row in project_items],
        "documents": [dict(row) for row in documents],
    }


def format_memory_context(context: dict[str, Any]) -> str:
    governed = context.get("governed_memories") or []
    docs = context.get("documents") or []
    items = context.get("project_items") or []
    if not governed and not docs and not items:
        return ""

    lines = [
        MEMORY_MARKER,
    ]
    if governed:
        lines.append(
            "以下是 DC-Agent 已通过 Obsidian 人工治理并批准的公司记忆。默认优先使用这些事实；回答中引用时要带来源。"
        )
        lines.append("已治理记忆：")
        for index, memory in enumerate(governed[:5], start=1):
            lines.append(
                f"{index}. memory_id={memory.get('memory_id') or ''}；"
                f"{memory.get('title') or ''}；项目={memory.get('project_id') or ''}；"
                f"负责人={memory.get('owner') or '未标注'}；"
                f"状态={memory.get('review_status') or ''}；"
                f"敏感级别={memory.get('sensitivity') or ''}；"
                f"置信度={memory.get('confidence') or ''}；"
                f"来源={memory.get('source_path') or memory.get('source_id') or ''}；"
                f"内容={str(memory.get('canonical_text') or '')[:500]}"
            )
    else:
        lines.extend(
            [
                "以下是 DC-Agent 从公司 NAS 记忆库检索到的资料。优先使用这些事实；如果 review_status=need_review，要明确说明仍需人工确认，不要编造缺失的发起人/负责人。",
                "温和复核策略：只有当本轮回答确实引用了状态为 need_review 的资料，且用户正在聊对应项目/资料时，才可以在回答末尾顺手加一句确认请求。不要主动批量追问，不要打断主任务。确认请求示例：另外，这份资料的 Obsidian 图谱归属还待人工确认；如果方便，麻烦顺手确认它是否属于「项目名」，负责人是否为「负责人/未标注」。用户可回复“确认无误”或“需要调整：...”。",
            ]
        )
    if items:
        lines.append("项目明细：")
        for index, item in enumerate(items[:5], start=1):
            evidence = _json_loads(str(item.get("evidence_json") or "{}"), {})
            raw = evidence.get("raw") if isinstance(evidence, dict) else None
            raw_hint = f"；原始行={raw[:6]}" if isinstance(raw, list) else ""
            lines.append(
                f"{index}. {item.get('project_name') or ''}；类型={item.get('project_type') or ''}；"
                f"状态={item.get('project_status') or ''}；负责人={item.get('owner') or '未标注'}；"
                f"部门={item.get('owner_department') or '未标注'}；来源={item.get('source_rel_path') or ''}{raw_hint}"
            )
    if docs:
        lines.append("相关文档：")
        for index, doc in enumerate(docs[:5], start=1):
            departments = ", ".join(
                _json_loads(str(doc.get("departments_json") or "[]"), [])
            )
            participants_data = _json_loads(
                str(doc.get("participants_json") or "[]"), []
            )
            participants = ", ".join(
                item.get("name", "")
                for item in participants_data
                if isinstance(item, dict)
            )
            excerpt = re.sub(
                r"\s+", " ", str(doc.get("summary") or doc.get("text") or "")
            )[:360]
            lines.append(
                f"{index}. doc_key={doc.get('doc_key') or ''}；"
                f"{doc.get('title') or ''}；项目={doc.get('project_name') or ''}；"
                f"类型={doc.get('doc_type') or ''}；负责人={doc.get('owner') or '未标注'}；"
                f"发起人={doc.get('initiator') or '未标注'}；部门={departments or '未标注'}；"
                f"相关人={participants or '未标注'}；状态={doc.get('review_status') or ''}；"
                f"来源={doc.get('rel_path') or ''}；摘要={excerpt}"
            )
    lines.append("</dc_agent_memory_context>")
    return "\n".join(lines)


def inject_memory_context_into_event(event) -> bool:
    platform_id = event.get_platform_id() or ""
    if platform_id not in SUPPORTED_PLATFORM_IDS:
        return False
    text = event.message_str or ""
    if MEMORY_MARKER in text:
        return False
    context = retrieve_memory_context(text)
    block = format_memory_context(context)
    if not block:
        return False

    merged = f"{text.strip()}\n\n{block}" if text.strip() else block
    event.message_str = merged
    try:
        event.message_obj.message_str = merged
    except Exception:  # noqa: BLE001
        pass
    try:
        event.set_extra("dc_agent_memory_context", context)
        event.set_extra(
            "dc_agent_memory_hits",
            {
                "documents": len(context.get("documents") or []),
                "governed_memories": len(context.get("governed_memories") or []),
                "project_items": len(context.get("project_items") or []),
            },
        )
    except Exception:  # noqa: BLE001
        pass
    return True
