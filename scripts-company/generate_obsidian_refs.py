#!/usr/bin/env python3
"""Generate Obsidian traceability pages from the NAS memory index.

The script is intentionally read-only for NAS and data/nas_memory.db. It only
writes Markdown pages under ObsidianVault.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DC_ROOT = Path("/Users/dianchi/DC-Agent")
DEFAULT_DB = DC_ROOT / "data" / "nas_memory.db"
DEFAULT_VAULT = DC_ROOT / "ObsidianVault"
DEFAULT_OVERRIDES = DC_ROOT / "data" / "config" / "nas_memory_overrides.json"
DEFAULT_REVIEW_CONFIRMATIONS = DC_ROOT / "data" / "obsidian_review_confirmations.jsonl"
DEFAULT_COMPANY_ORG = DC_ROOT / "data" / "config" / "company_org_structure.json"
DEFAULT_ENTITY_TAXONOMY = DC_ROOT / "data" / "config" / "company_entity_taxonomy.json"
DEFAULT_PROJECT_NORMALIZATION = (
    DC_ROOT / "data" / "config" / "company_project_normalization.json"
)
DEFAULT_STALE_RAW_REF_ARCHIVE = DC_ROOT / "data" / "obsidian_stale_rawrefs"
GRAPH_CONFIG = {
    "collapse-filter": True,
    "search": "path:10_Index",
    "showTags": False,
    "showAttachments": False,
    "hideUnresolved": True,
    "showOrphans": False,
    "collapse-color-groups": False,
    "colorGroups": [],
    "collapse-display": True,
    "showArrow": False,
    "textFadeMultiplier": 0,
    "nodeSizeMultiplier": 1,
    "lineSizeMultiplier": 1,
    "collapse-forces": True,
    "centerStrength": 0.52,
    "repelStrength": 10,
    "linkStrength": 1,
    "linkDistance": 180,
    "scale": 1,
    "close": True,
}
CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def clean_text(value: Any) -> str:
    text = "" if value is None else str(value)
    text = CONTROL_CHARS_RE.sub(" ", text)
    return text.replace("\r\n", "\n").replace("\r", "\n")


def clean_json(value: Any) -> Any:
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, list):
        return [clean_json(item) for item in value]
    if isinstance(value, dict):
        return {clean_text(key): clean_json(item) for key, item in value.items()}
    return value


def sanitize_filename(value: str, fallback: str) -> str:
    value = clean_text(value)
    name = re.sub(r"[\\/:*?\"<>|#^[\\]]+", " ", value).strip()
    name = re.sub(r"\s+", " ", name)
    return name[:120].strip() or fallback


def yaml_scalar(value: Any) -> str:
    return json.dumps(clean_text(value), ensure_ascii=False)


def load_json_array(value: str) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if str(item).strip()]


def load_json_dict(value: str) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
    return records


def load_people(value: str) -> list[dict[str, str]]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []

    people: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in parsed:
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            department = str(item.get("department") or "").strip()
            role = str(item.get("role") or "").strip()
        else:
            name = str(item).strip()
            department = ""
            role = ""
        if not name or name in seen:
            continue
        seen.add(name)
        people.append({"name": name, "department": department, "role": role})
    return people


def unique_values(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        cleaned = str(value).strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        result.append(cleaned)
    return result


def org_people_names(org_model: dict[str, Any]) -> set[str]:
    return set((org_model.get("people") or {}).keys())


def department_aliases(org_model: dict[str, Any]) -> dict[str, str]:
    departments = org_model.get("departments") or {}
    aliases: dict[str, str] = {}
    for name, item in departments.items():
        aliases[name] = name
        if isinstance(item, dict):
            for alias in item.get("aliases") or []:
                cleaned = str(alias).strip()
                if cleaned:
                    aliases[cleaned] = name
    return aliases


def normalize_department(name: str, org_model: dict[str, Any]) -> str:
    cleaned = str(name or "").strip()
    if not cleaned:
        return ""
    return department_aliases(org_model).get(cleaned, cleaned)


def normalize_departments(names: list[str], org_model: dict[str, Any]) -> list[str]:
    aliases = department_aliases(org_model)
    normalized = [aliases.get(str(name).strip(), str(name).strip()) for name in names]
    return unique_values(normalized)


def person_ref(name: str, org_model: dict[str, Any]) -> str:
    cleaned = str(name or "").strip()
    if not cleaned:
        return "待确认"
    if cleaned in org_people_names(org_model):
        return f"[[{cleaned}]]"
    return f"{cleaned}（未进入正式人员图谱）"


def department_ref(name: str, org_model: dict[str, Any]) -> str:
    cleaned = str(name or "").strip()
    if not cleaned:
        return "待确认"
    normalized = normalize_department(cleaned, org_model)
    if normalized in (org_model.get("departments") or {}):
        if normalized == cleaned:
            return f"[[{normalized}]]"
        return f"[[{normalized}]]（原始：{cleaned}）"
    return f"{cleaned}（未进入正式部门图谱）"


def chunk_stats(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "select doc_key, count(*) as chunk_count from chunks group by doc_key"
    ).fetchall()
    return {row["doc_key"]: int(row["chunk_count"]) for row in rows}


def first_chunk_preview(conn: sqlite3.Connection, doc_key: str) -> str:
    row = conn.execute(
        """
        select text
        from chunks
        where doc_key = ?
        order by chunk_index asc
        limit 1
        """,
        (doc_key,),
    ).fetchone()
    if row is None:
        return ""
    text = re.sub(r"\s+", " ", clean_text(row["text"])).strip()
    return text[:320]


def query_documents(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        select
            doc_key,
            rel_path,
            source_path,
            sha256,
            file_size,
            parser,
            title,
            summary,
            tags_json,
            indexed_at,
            metadata_json,
            archive_path,
            project_id,
            project_name,
            doc_type,
            initiator,
            owner,
            departments_json,
            participants_json,
            confidence,
            review_status
        from documents
        order by indexed_at desc, title asc
        """
    ).fetchall()


def render_raw_ref(
    row: sqlite3.Row,
    title: str,
    chunk_count: int,
    preview: str,
    org_model: dict[str, Any],
    project_aliases: dict[str, str],
) -> str:
    tags = load_json_array(row["tags_json"])
    people = load_people(row["participants_json"])
    departments = normalize_departments(
        load_json_array(row["departments_json"])
        + [person["department"] for person in people if person["department"]],
        org_model,
    )
    metadata = clean_json(load_json_dict(row["metadata_json"]))
    file_size = int(row["file_size"])
    size_kib = file_size / 1024
    summary = clean_text(row["summary"] or "待补充。")
    preview = clean_text(preview or "暂无 chunk 预览。")
    raw_project_name = str(row["project_name"] or "").strip()
    project_name = normalize_project_name(raw_project_name, project_aliases)
    private_technical = is_private_technical_text(
        row["title"],
        row["rel_path"],
        row["doc_type"],
        raw_project_name,
    )

    lines = [
        "---",
        f"title: {yaml_scalar(title)}",
        f"doc_key: {yaml_scalar(row['doc_key'])}",
        f"source_path: {yaml_scalar(row['source_path'])}",
        f"rel_path: {yaml_scalar(row['rel_path'])}",
        f"sha256: {yaml_scalar(row['sha256'])}",
        f"parser: {yaml_scalar(row['parser'])}",
        f"doc_type: {yaml_scalar(row['doc_type'])}",
        f"project_id: {yaml_scalar(row['project_id'])}",
        f"project_name: {yaml_scalar(project_name)}",
        f"raw_project_name: {yaml_scalar(raw_project_name)}",
        f"owner: {yaml_scalar(row['owner'])}",
        f"review_status: {yaml_scalar(row['review_status'])}",
        f"confidence: {float(row['confidence'] or 0):.3f}",
        f"indexed_at: {yaml_scalar(row['indexed_at'])}",
        f"file_size_bytes: {file_size}",
        f"chunk_count: {chunk_count}",
        "tags:",
    ]
    lines.extend(f"  - {yaml_scalar(tag)}" for tag in tags)
    lines.extend(
        [
            "---",
            "",
            f"# {title}",
            "",
            "## 来源",
            "",
            f"- 原始路径：`{row['source_path']}`",
            f"- 相对路径：`{row['rel_path']}`",
            f"- 解析器：`{row['parser']}`",
            f"- 文件大小：{size_kib:.1f} KiB",
            f"- SHA256：`{row['sha256']}`",
            f"- 索引时间：`{row['indexed_at']}`",
            "",
            "## 知识库状态",
            "",
            f"- 文档类型：{row['doc_type'] or '待确认'}",
            f"- 项目：{project_name or '待确认'}",
            f"- 负责人：{row['owner'] or '待确认'}",
            f"- 复核状态：{row['review_status'] or 'need_review'}",
            f"- chunk 数：{chunk_count}",
            "",
            "## 摘要",
            "",
            summary,
            "",
            "## 正文预览",
            "",
            preview,
            "",
            "## 关联",
            "",
        ]
    )
    if raw_project_name and project_name and raw_project_name != project_name:
        lines.append(f"- 原始项目名：{raw_project_name}")
    if private_technical:
        lines.append("- 公司图谱：技术资料隔离，不生成项目双链")
    elif project_name:
        lines.append(f"- 项目：[[{project_name}]]")
    if row["doc_type"]:
        lines.append(f"- 文档类型：[[{row['doc_type']}]]")
    if row["owner"]:
        lines.append(f"- 负责人：{person_ref(row['owner'], org_model)}")
    for department in departments:
        lines.append(f"- 部门：{department_ref(department, org_model)}")
    for person in people:
        department = normalize_department(person["department"], org_model)
        detail = (
            f"（{department} / {person['role']}）"
            if department or person["role"]
            else ""
        )
        lines.append(f"- 参与人：{person_ref(person['name'], org_model)}{detail}")
    if (
        not any(row[key] for key in ("project_name", "doc_type", "owner"))
        and not departments
    ):
        lines.append("- [[待人工确认]]")

    lines.extend(
        [
            "",
            "## 元数据",
            "",
            "```json",
            json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True),
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def render_total_index(docs: list[dict[str, Any]], generated_at: str) -> str:
    by_type = Counter(doc["doc_type"] or "待确认" for doc in docs)
    by_status = Counter(doc["review_status"] or "need_review" for doc in docs)
    by_parser = Counter(doc["parser"] or "unknown" for doc in docs)

    lines = [
        "# 公司知识库总索引",
        "",
        f"生成时间：`{generated_at}`",
        "",
        "## 第一阶段报告",
        "",
        "- [[DC-Agent知识库能力诊断报告]]",
        "- [[文档资产诊断报告]]",
        "- [[分类重构设计树]]",
        "- [[DC-Agent批量导入执行计划]]",
        "- [[Obsidian关系图谱与资料溯源设计]]",
        "- [[NAS知识库同步闭环修复报告]]",
        "- [[Obsidian图谱清理与全量RawRef扩展报告]]",
        "- [[业务关系规则确认报告]]",
        "- [[实体关系清洗确认报告]]",
        "- [[实体抽查与口径收紧报告]]",
        "- [[项目节点清洗确认报告]]",
        "",
        "## 第二阶段全量 RawRef",
        "",
        f"- RawRef 文档数：{len(docs)}",
        f"- 复核状态：{', '.join(f'{k} {v}' for k, v in sorted(by_status.items()))}",
        f"- 解析器分布：{', '.join(f'{k} {v}' for k, v in sorted(by_parser.items()))}",
        "- 干净图谱入口：[[知识地图]]",
        "",
        "## 文档类型",
        "",
    ]
    lines.extend(
        f"- [[{doc_type}]]：{count}" for doc_type, count in sorted(by_type.items())
    )
    lines.extend(
        [
            "",
            "## RawRefs",
            "",
        ]
    )
    lines.extend(
        f"- [[{doc['link_title']}]] - {doc['doc_type'] or '待确认'} - `{doc['rel_path']}`"
        for doc in docs
    )
    lines.extend(
        [
            "",
            "## 知识入口",
            "",
            "- [[产品线]]",
            "- [[项目]]",
            "- [[客户]]",
            "- [[市场与品牌]]",
            "- [[待人工确认]]",
            "- [[员工确认记录]]",
            "",
        ]
    )
    return "\n".join(lines)


def render_clean_knowledge_map(docs: list[dict[str, Any]], generated_at: str) -> str:
    by_status = Counter(doc["review_status"] or "need_review" for doc in docs)
    by_parser = Counter(doc["parser"] or "unknown" for doc in docs)
    lines = [
        "# 知识地图",
        "",
        f"生成时间：`{generated_at}`",
        "",
        "推荐优先打开旁边的 Canvas：",
        "",
        "- [[公司知识地图.canvas]]",
        "",
        "这个 Markdown 页面是给全局图谱使用的干净入口，只保留高层关系。RawRefs、项目明细、人员明细仍然存在，但不默认塞进全局图谱。",
        "",
        "## 当前主库",
        "",
        f"- RawRefs：{len(docs)}",
        f"- 复核状态：{', '.join(f'{k} {v}' for k, v in sorted(by_status.items()))}",
        f"- 解析器分布：{', '.join(f'{k} {v}' for k, v in sorted(by_parser.items()))}",
        "",
        "## 主干",
        "",
        "- [[产品客户图谱]]",
        "- [[业务主题图谱]]",
        "- [[人员部门图谱]]",
        "- [[文档类型图谱]]",
        "",
        "## 快速入口",
        "",
        "- [[公司知识库总索引]]",
        "- [[来源文档]]",
        "- [[复核工作台]]",
        "- [[员工确认记录]]",
        "- [[规则确认]]",
        "- [[产品线]]",
        "- [[项目]]",
        "- [[客户]]",
        "- [[市场与品牌]]",
        "- [[待人工确认]]",
        "",
    ]
    return "\n".join(lines)


def render_review_bridge(docs: list[dict[str, Any]], generated_at: str) -> str:
    lines = [
        "# 待人工确认",
        "",
        f"生成时间：`{generated_at}`",
        "",
        "这些文档已进入 `data/nas_memory.db`，但还没有完成业务归类复核。",
        "",
        "## 待复核文档",
        "",
    ]
    for doc in docs:
        if doc["review_status"] == "need_review":
            lines.append(
                f"- [[{doc['link_title']}]] - {doc['doc_type'] or '待确认'} - `{doc['rel_path']}`"
            )
    lines.append("")
    return "\n".join(lines)


def render_rule_confirmed_bridge(docs: list[dict[str, Any]], generated_at: str) -> str:
    lines = [
        "# 规则确认",
        "",
        f"生成时间：`{generated_at}`",
        "",
        "这些文档已满足保守规则，从人工待办队列移出；它们不是人工确认，依据保存在 RawRef 的 `metadata.rule_confirmation`。",
        "",
        "## 规则确认文档",
        "",
    ]
    for doc in docs:
        if doc["review_status"] == "rule_confirmed":
            lines.append(
                f"- [[{doc['link_title']}]] - {doc['doc_type'] or '待确认'} - `{doc['rel_path']}`"
            )
    lines.append("")
    return "\n".join(lines)


def p0_review_candidates(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        doc
        for doc in docs
        if doc["review_status"] == "need_review"
        and (doc["parser"] in {"docx", "md"} or doc["doc_type"] in {"SOP", "执行方案"})
    ][:50]


def render_review_workbench(docs: list[dict[str, Any]], generated_at: str) -> str:
    by_status = Counter(doc["review_status"] or "need_review" for doc in docs)
    candidates = p0_review_candidates(docs)
    lines = [
        "# 复核工作台",
        "",
        f"生成时间：`{generated_at}`",
        "",
        "## 当前口径",
        "",
        "- 只列入公司图谱文档；开发/技术资料已隔离，不进入本工作台。",
        "- P0 优先选择 docx/md 或 SOP/执行方案，便于先做问答与归属复核。",
        "",
        "## 状态分布",
        "",
    ]
    lines.extend(f"- {name}：{count}" for name, count in sorted(by_status.items()))
    lines.extend(
        [
            "",
            "## P0 候选",
            "",
            "| 状态 | 文档 | 类型 | 解析器 | 来源 |",
            "|---|---|---|---|---|",
        ]
    )
    for doc in candidates:
        lines.append(
            f"| [ ] | [[{doc['link_title']}]] | {doc['doc_type'] or '待确认'} | {doc['parser']} | `{doc['rel_path']}` |"
        )
    lines.extend(["", "P0 明细：[[P0-优先复核]]", ""])
    return "\n".join(lines)


def render_p0_review_bridge(docs: list[dict[str, Any]], generated_at: str) -> str:
    candidates = p0_review_candidates(docs)
    lines = [
        "# P0-优先复核",
        "",
        f"生成时间：`{generated_at}`",
        "",
        "## 候选文档",
        "",
        "| 状态 | 文档 | 类型 | 解析器 | 来源 |",
        "|---|---|---|---|---|",
    ]
    for doc in candidates:
        lines.append(
            f"| [ ] | [[{doc['link_title']}]] | {doc['doc_type'] or '待确认'} | {doc['parser']} | `{doc['rel_path']}` |"
        )
    lines.append("")
    return "\n".join(lines)


def render_employee_review_confirmations(
    records: list[dict[str, Any]],
    generated_at: str,
) -> str:
    lines = [
        "# 员工确认记录",
        "",
        f"生成时间：`{generated_at}`",
        "",
        "这里汇总小助手在相关对话中顺手收集到的 Obsidian 图谱复核反馈。它是人工复核依据，不等同于已写回 `data/nas_memory.db` 的 confirmed 状态。",
        "",
        f"- 记录数：{len(records)}",
        "",
        "## 最近记录",
        "",
    ]
    if not records:
        lines.extend(["暂无员工确认记录。", ""])
        return "\n".join(lines)

    sorted_records = sorted(
        records,
        key=lambda item: str(item.get("created_at") or ""),
        reverse=True,
    )
    for record in sorted_records[:200]:
        fields = record.get("parsed_fields")
        fields = fields if isinstance(fields, dict) else {}
        candidates = record.get("candidates")
        candidates = candidates if isinstance(candidates, list) else []
        candidate_links = []
        for candidate in candidates[:5]:
            if not isinstance(candidate, dict):
                continue
            title = str(
                candidate.get("title")
                or candidate.get("rel_path")
                or candidate.get("doc_key")
                or ""
            ).strip()
            if title:
                candidate_links.append(f"[[{sanitize_filename(title, title)}]]")
        lines.extend(
            [
                f"### {record.get('created_at') or '未知时间'} · {record.get('sender_name') or record.get('sender_id') or '未知员工'}",
                "",
                f"- 动作：{record.get('action') or 'unknown'}",
                f"- 绑定候选：{', '.join(candidate_links) if candidate_links else '未绑定，需人工从上下文判断'}",
                f"- 原始回复：{clean_text(record.get('raw_text') or '')}",
            ]
        )
        if fields:
            lines.append("- 解析字段：")
            for key, value in sorted(fields.items()):
                lines.append(f"  - {key}: {clean_text(value)}")
        lines.append("")
    return "\n".join(lines)


def render_doc_type_bridge(
    doc_type: str, docs: list[dict[str, Any]], generated_at: str
) -> str:
    lines = [
        f"# {doc_type}",
        "",
        f"生成时间：`{generated_at}`",
        "",
        "## 关联文档",
        "",
    ]
    for doc in docs:
        lines.append(f"- [[{doc['link_title']}]] - `{doc['rel_path']}`")
    lines.append("")
    return "\n".join(lines)


def render_entity_bridge(
    kind: str, name: str, docs: list[dict[str, Any]], generated_at: str
) -> str:
    lines = [
        f"# {name}",
        "",
        f"类型：{kind}",
        f"生成时间：`{generated_at}`",
        "",
        "## 关联文档",
        "",
    ]
    for doc in docs:
        lines.append(
            f"- [[{doc['link_title']}]] - {doc['doc_type'] or '待确认'} - `{doc['rel_path']}`"
        )
    lines.append("")
    return "\n".join(lines)


def clear_generated_markdown(directory: Path) -> None:
    for path in directory.glob("*.md"):
        path.unlink()


def render_graph_entry(
    docs: list[dict[str, Any]],
    by_type: dict[str, list[dict[str, Any]]],
    by_project: dict[str, list[dict[str, Any]]],
    by_person: dict[str, list[dict[str, Any]]],
    by_department: dict[str, list[dict[str, Any]]],
    generated_at: str,
) -> str:
    org_model = load_org_model()
    known_people = {
        name: items
        for name, items in by_person.items()
        if name in (org_model.get("people") or {})
    }
    known_departments = {
        name: items
        for name, items in by_department.items()
        if name in (org_model.get("departments") or {})
    }
    lines = [
        "# 知识图谱入口",
        "",
        f"生成时间：`{generated_at}`",
        "",
        "## 总览",
        "",
        f"- RawRefs：{len(docs)}",
        f"- 文档类型节点：{len(by_type)}",
        f"- 项目节点：{len(by_project)}",
        f"- 已确认人员节点：{len(known_people)} / 原始人名 {len(by_person)}",
        f"- 已确认部门节点：{len(known_departments)} / 原始部门 {len(by_department)}",
        "",
        "## 文档类型",
        "",
    ]
    lines.extend(
        f"- [[{name}]]：{len(items)}" for name, items in sorted(by_type.items())
    )
    lines.extend(["", "## 项目", ""])
    lines.extend(
        f"- [[{name}]]：{len(items)}" for name, items in sorted(by_project.items())
    )
    lines.extend(["", "## 人员（已确认组织人员）", ""])
    lines.extend(
        f"- [[{name}]]：{len(items)}" for name, items in sorted(known_people.items())
    )
    lines.extend(["", "## 部门（已确认组织部门）", ""])
    lines.extend(
        f"- [[{name}]]：{len(items)}"
        for name, items in sorted(known_departments.items())
    )
    lines.extend(
        [
            "",
            "## 隔离口径",
            "",
            f"- 未确认人名：{len(by_person) - len(known_people)}，不在本入口展开。",
            f"- 未确认部门/角色：{len(by_department) - len(known_departments)}，不在本入口展开。",
        ]
    )
    lines.append("")
    return "\n".join(lines)


def render_link_index(
    title: str, intro: str, links: list[str], generated_at: str
) -> str:
    lines = [
        f"# {title}",
        "",
        f"生成时间：`{generated_at}`",
        "",
        intro,
        "",
        "## 关联节点",
        "",
    ]
    if links:
        lines.extend(f"- [[{link}]]" for link in unique_values(sorted(links)))
    else:
        lines.append("- [[待人工确认]]")
    lines.append("")
    return "\n".join(lines)


def render_focus_map(
    title: str, intro: str, sections: list[tuple[str, list[str]]], generated_at: str
) -> str:
    lines = [
        f"# {title}",
        "",
        f"生成时间：`{generated_at}`",
        "",
        intro,
        "",
    ]
    for section_title, links in sections:
        lines.extend([f"## {section_title}", ""])
        if links:
            lines.extend(f"- [[{link}]]" for link in unique_values(links))
        else:
            lines.append("- [[待人工确认]]")
        lines.append("")
    return "\n".join(lines)


def entity_link_name(name: str) -> str:
    return f"实体-{name}"


def doc_haystack(doc: dict[str, Any]) -> str:
    return " ".join(
        clean_text(doc.get(key) or "")
        for key in (
            "link_title",
            "title",
            "rel_path",
            "doc_type",
            "project_name",
        )
    ).lower()


PRIVATE_TECHNICAL_KEYWORDS = (
    "Method_C",
    "DC-Agent",
    "Codex",
    "Harness",
    "openclaw",
    "Openclaw",
    "API权限申请",
    "巅池-技术日报",
    "飞书AI机器人功能规划",
    "飞书AI机器人对接技术",
)


def is_private_technical_text(*values: object) -> bool:
    haystack = " ".join(clean_text(str(value or "")) for value in values)
    haystack_lower = haystack.lower()
    return any(
        keyword.lower() in haystack_lower for keyword in PRIVATE_TECHNICAL_KEYWORDS
    )


def load_entity_taxonomy(path: Path = DEFAULT_ENTITY_TAXONOMY) -> dict[str, Any]:
    if not path.exists():
        return {"entity_types": {}}
    parsed = json.loads(path.read_text(encoding="utf-8"))
    return parsed if isinstance(parsed, dict) else {"entity_types": {}}


def load_project_normalization(
    path: Path = DEFAULT_PROJECT_NORMALIZATION,
) -> dict[str, str]:
    if not path.exists():
        return {}
    parsed = json.loads(path.read_text(encoding="utf-8"))
    projects = parsed.get("projects") if isinstance(parsed, dict) else {}
    if not isinstance(projects, dict):
        return {}
    aliases: dict[str, str] = {}
    for canonical, details in projects.items():
        canonical_name = str(canonical).strip()
        if not canonical_name:
            continue
        aliases[canonical_name] = canonical_name
        if isinstance(details, dict):
            for alias in details.get("aliases") or []:
                alias_name = str(alias).strip()
                if alias_name:
                    aliases[alias_name] = canonical_name
    return aliases


def normalize_project_name(name: str, project_aliases: dict[str, str]) -> str:
    cleaned = str(name or "").strip()
    if not cleaned:
        return ""
    return project_aliases.get(cleaned, cleaned)


def match_entity_docs(
    docs: list[dict[str, Any]], entity: dict[str, Any]
) -> list[dict[str, Any]]:
    keywords = [
        str(value).strip().lower()
        for value in entity.get("keywords", [])
        if str(value).strip()
    ]
    aliases = [
        str(value).strip().lower()
        for value in entity.get("aliases", [])
        if str(value).strip()
    ]
    name = str(entity.get("name") or "").strip().lower()
    needles = unique_values([name, *keywords, *aliases])
    if not needles:
        return []
    matched: list[dict[str, Any]] = []
    for doc in docs:
        haystack = doc_haystack(doc)
        if any(needle and needle in haystack for needle in needles):
            matched.append(doc)
    return matched


def build_entity_matches(
    docs: list[dict[str, Any]],
    taxonomy: dict[str, Any],
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    matches: dict[str, dict[str, list[dict[str, Any]]]] = {}
    entity_types = taxonomy.get("entity_types") or {}
    if not isinstance(entity_types, dict):
        return matches
    for entity_type, entities in entity_types.items():
        if not isinstance(entities, list):
            continue
        for entity in entities:
            if not isinstance(entity, dict):
                continue
            name = str(entity.get("name") or "").strip()
            if not name:
                continue
            entity_docs = match_entity_docs(docs, entity)
            if entity_docs:
                matches.setdefault(str(entity_type), {})[name] = entity_docs
    return matches


def render_entity_taxonomy_bridge(
    entity_type: str,
    entity_name: str,
    docs: list[dict[str, Any]],
    generated_at: str,
) -> str:
    by_type = Counter(doc["doc_type"] or "待确认" for doc in docs)
    by_project = Counter(doc["project_name"] or "待确认项目" for doc in docs)
    lines = [
        f"# {entity_name}",
        "",
        f"类型：{entity_type}",
        f"生成时间：`{generated_at}`",
        "",
        "## 关系确认",
        "",
        "- 来源：`data/config/company_entity_taxonomy.json` 的确定性关键词匹配。",
        "- 本页是实体桥接页，不改 NAS 原始文件，不改 `nas_memory.db`。",
        "",
        "## 文档类型分布",
        "",
    ]
    lines.extend(f"- [[{name}]]：{count}" for name, count in sorted(by_type.items()))
    lines.extend(["", "## 项目 Top", ""])
    lines.extend(f"- [[{name}]]：{count}" for name, count in by_project.most_common(20))
    lines.extend(["", "## 关联 RawRefs", ""])
    for doc in docs:
        lines.append(
            f"- [[{doc['link_title']}]] - {doc['doc_type'] or '待确认'} - `{doc['rel_path']}`"
        )
    lines.append("")
    return "\n".join(lines)


def render_entity_relation_report(
    entity_matches: dict[str, dict[str, list[dict[str, Any]]]],
    taxonomy: dict[str, Any],
    generated_at: str,
) -> str:
    lines = [
        "# 实体关系清洗确认报告",
        "",
        f"生成时间：`{generated_at}`",
        "",
        "## 结论",
        "",
        "本轮只做确定性实体清洗：用配置中的产品线、车型/品牌资产和业务主题关键词，把 RawRef 挂到实体桥接页。",
        "",
        "## 实体覆盖",
        "",
        "| 类型 | 实体 | 命中文档数 |",
        "|---|---|---:|",
    ]
    for entity_type, entities in sorted(entity_matches.items()):
        for entity_name, docs in sorted(entities.items()):
            lines.append(
                f"| {entity_type} | [[{entity_link_name(entity_name)}]] | {len(docs)} |"
            )
    lines.extend(
        [
            "",
            "## 口径",
            "",
            "- 只对配置内实体建桥接页，避免把文件名里的每个词都变成节点。",
            "- 项目节点仍保留为三级下钻层，但主图优先走实体桥接页。",
            "- 未命中的项目不强行归类，继续留在 [[项目]] 和 [[复核工作台]]。",
            "",
            "## 配置来源",
            "",
            f"- `{DEFAULT_ENTITY_TAXONOMY}`",
            "",
        ]
    )
    return "\n".join(lines)


def render_source_docs_index(docs: list[dict[str, Any]], generated_at: str) -> str:
    lines = [
        "# 来源文档",
        "",
        f"生成时间：`{generated_at}`",
        "",
        "这里说明 RawRef 的使用口径。`00_RawRefs` 已按 `data/nas_memory.db` 全量生成，但默认全局图谱不显示 RawRef，避免 900+ 来源节点把图谱铺乱。",
        "",
        "## 当前覆盖",
        "",
        f"- RawRef 数：{len(docs)}",
        "- 来源字段：`source_path`、`rel_path`、`sha256`、`doc_key`",
        "- 下钻方式：从 [[公司知识库总索引]]、[[复核工作台]] 或任意桥接页打开具体 RawRef。",
        "",
        "## 引用规则",
        "",
        "当 Agent 或报告写到 `[[来源文档]]` 时，实际输出应替换为具体 RawRef 页面，例如：",
        "",
    ]
    for doc in docs[:8]:
        lines.append(f"- [[{doc['link_title']}]]")
    lines.extend(
        [
            "",
            "如果还没有对应 RawRef，则必须输出完整 `source_path`。",
            "",
        ]
    )
    return "\n".join(lines)


def top_links(groups: dict[str, list[dict[str, Any]]], limit: int = 20) -> list[str]:
    return [
        name
        for name, _items in sorted(
            groups.items(),
            key=lambda item: (-len(item[1]), item[0]),
        )[:limit]
    ]


def collect_org_departments(
    nodes: list[dict[str, Any]], parent: str = ""
) -> dict[str, dict[str, Any]]:
    departments: dict[str, dict[str, Any]] = {}
    for node in nodes:
        name = str(node.get("name") or "").strip()
        if not name:
            continue
        children = node.get("children") or []
        aliases = [
            str(value).strip()
            for value in node.get("aliases") or []
            if str(value).strip()
        ]
        departments[name] = {
            "lead": "",
            "scope": [
                str(child.get("name") or "").strip()
                for child in children
                if isinstance(child, dict) and str(child.get("name") or "").strip()
            ],
            "description": f"上级：{parent}" if parent else "公司一级组织",
            "parent": parent,
            "aliases": aliases,
            "children": [
                str(child.get("name") or "").strip()
                for child in children
                if isinstance(child, dict) and str(child.get("name") or "").strip()
            ],
        }
        departments.update(
            collect_org_departments(
                [child for child in children if isinstance(child, dict)], name
            )
        )
    return departments


def load_company_org_model(path: Path = DEFAULT_COMPANY_ORG) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None

    roots = [node for node in parsed.get("roots") or [] if isinstance(node, dict)]
    departments = collect_org_departments(roots)
    people: dict[str, dict[str, str]] = {}
    raw_people = parsed.get("people") or {}
    if isinstance(raw_people, dict):
        for name, item in raw_people.items():
            person = str(name or "").strip()
            if not person:
                continue
            details = item if isinstance(item, dict) else {}
            department = str(details.get("department") or "").strip()
            role = str(details.get("role") or "").strip()
            office = str(details.get("office") or "").strip()
            people[person] = {"department": department, "role": role, "office": office}
            if department and department not in departments:
                departments[department] = {
                    "lead": "",
                    "scope": [],
                    "description": "由人员表自动补充。",
                    "parent": "",
                    "aliases": [],
                    "children": [],
                }

    for name, info in people.items():
        department = info.get("department", "")
        role = info.get("role", "")
        if not department or department not in departments:
            continue
        if departments[department].get("lead"):
            continue
        if any(keyword in role for keyword in ("总经理", "总监", "经理", "主管")):
            departments[department]["lead"] = name

    return {
        "departments": departments,
        "people": people,
        "roots": roots,
        "source": parsed.get("source") or {},
    }


def load_org_model(path: Path = DEFAULT_OVERRIDES) -> dict[str, Any]:
    company = load_company_org_model()
    if company is not None:
        return company
    if not path.exists():
        return {"departments": {}, "people": {}}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"departments": {}, "people": {}}
    if not isinstance(parsed, dict):
        return {"departments": {}, "people": {}}

    departments: dict[str, dict[str, Any]] = {}
    raw_departments = parsed.get("departments") or {}
    if isinstance(raw_departments, dict):
        for name, item in raw_departments.items():
            department = str(name or "").strip()
            if not department:
                continue
            details = item if isinstance(item, dict) else {}
            departments[department] = {
                "lead": str(details.get("lead") or "").strip(),
                "scope": [
                    str(value).strip()
                    for value in details.get("scope") or []
                    if str(value).strip()
                ],
                "description": str(details.get("description") or "").strip(),
            }

    people: dict[str, dict[str, str]] = {}
    raw_people = parsed.get("people") or {}
    if isinstance(raw_people, dict):
        for name, item in raw_people.items():
            person = str(name or "").strip()
            if not person:
                continue
            details = item if isinstance(item, dict) else {}
            department = str(details.get("department") or "").strip()
            role = str(details.get("role") or "").strip()
            people[person] = {"department": department, "role": role}
            if department and department not in departments:
                departments[department] = {
                    "lead": "",
                    "scope": [],
                    "description": "由已确认人员归属自动补充。",
                }

    for department, details in list(departments.items()):
        lead = str(details.get("lead") or "").strip()
        if lead and lead not in people:
            people[lead] = {"department": department, "role": "部门负责人"}

    return {"departments": departments, "people": people}


def render_personnel_department_map(
    org_model: dict[str, Any],
    by_person: dict[str, list[dict[str, Any]]],
    by_department: dict[str, list[dict[str, Any]]],
    generated_at: str,
) -> str:
    departments: dict[str, dict[str, Any]] = org_model.get("departments") or {}
    people: dict[str, dict[str, str]] = org_model.get("people") or {}
    roots: list[dict[str, Any]] = org_model.get("roots") or []

    def render_tree(nodes: list[dict[str, Any]], level: int = 0) -> list[str]:
        rendered: list[str] = []
        indent = "  " * level
        for node in nodes:
            name = str(node.get("name") or "").strip()
            if not name:
                continue
            rendered.append(f"{indent}- [[{name}]]")
            children = [
                child for child in node.get("children") or [] if isinstance(child, dict)
            ]
            rendered.extend(render_tree(children, level + 1))
        return rendered

    by_known_department: dict[str, list[tuple[str, dict[str, str]]]] = defaultdict(list)
    for name, info in sorted(
        people.items(), key=lambda item: (item[1].get("department", ""), item[0])
    ):
        department = str(info.get("department") or "待确认").strip() or "待确认"
        by_known_department[department].append((name, info))

    unknown_people = [
        name
        for name in sorted(by_person)
        if name not in people and not re.search(r"[、,，/]", name)
    ]
    composite_people = [
        name for name in sorted(by_person) if re.search(r"[、,，/]", name)
    ]

    lines = [
        "# 人员部门图谱",
        "",
        f"生成时间：`{generated_at}`",
        "",
        "本页只展示已确认组织人员、部门负责人和复核入口；普通出现过的人名、客户昵称、账号作者、拍摄对象、执行素材人员不进入人员主图。",
        "",
        "## 组织架构",
        "",
        *(render_tree(roots) if roots else ["- [[待人工确认]]"]),
        "",
        "## 组织部门",
        "",
        "| 部门 | 上级 | 负责人 | 下级/范围 | 别名 |",
        "|---|---|---|---|---|",
    ]
    for department, info in sorted(departments.items()):
        lead = str(info.get("lead") or "").strip()
        scope = "、".join(info.get("scope") or [])
        parent = str(info.get("parent") or "").strip()
        aliases = "、".join(info.get("aliases") or [])
        lines.append(
            f"| [[{department}]] | {f'[[{parent}]]' if parent else '—'} | {f'[[{lead}]]' if lead else '待确认'} | {scope or '—'} | {aliases or '—'} |"
        )

    lines.extend(["", "## 已确认人员（按部门）", ""])
    for department, entries in sorted(by_known_department.items()):
        lines.extend([f"### {department}", ""])
        lines.append("| 人员 | 角色 | 关联文档数 |")
        lines.append("|---|---|---:|")
        for name, info in entries:
            role = str(info.get("role") or "待确认").strip()
            lines.append(f"| [[{name}]] | {role} | {len(by_person.get(name, []))} |")
        lines.append("")

    lines.extend(
        [
            "## 候选与复核入口",
            "",
            "- [[agy候选规则]]：agy 候选与本地规则共同通过后的缓冲队列。",
            "- [[P4-待补人员规则]]：有项目总表证据但缺人员/部门规则的候选。",
            "- [[待人工确认]]：证据不足或无法稳定归属的文档。",
            "",
            "## 隔离口径",
            "",
            f"- 未确认单人名数量：{len(unknown_people)}；不在本页展开，避免把客户、账号作者、拍摄对象或执行人员误连成组织人员。",
            f"- 组合人名数量：{len(composite_people)}；例如 `A、B` 这种组合不作为人员节点进入主图。",
            "- 如果某个人需要进入主图，先补充到 `data/config/company_org_structure.json` 的 `people`，再重建 Obsidian。",
            "",
        ]
    )
    return "\n".join(lines)


def docs_matching(docs: list[dict[str, Any]], patterns: list[str]) -> list[str]:
    matched: list[str] = []
    regexes = [re.compile(pattern, re.I) for pattern in patterns]
    for doc in docs:
        haystack = " ".join(
            str(doc.get(key) or "")
            for key in ("link_title", "rel_path", "doc_type", "project_name")
        )
        if any(regex.search(haystack) for regex in regexes):
            matched.append(doc["link_title"])
    return matched


def archive_stale_raw_refs(raw_refs: Path, expected_files: set[str]) -> int:
    stale_files = [
        path for path in raw_refs.glob("*.md") if path.name not in expected_files
    ]
    if not stale_files:
        return 0

    archive_dir = DEFAULT_STALE_RAW_REF_ARCHIVE / datetime.now(UTC).strftime(
        "%Y%m%d%H%M%S"
    )
    archive_dir.mkdir(parents=True, exist_ok=True)
    for path in stale_files:
        destination = archive_dir / path.name
        index = 2
        while destination.exists():
            destination = archive_dir / f"{path.stem}-{index}{path.suffix}"
            index += 1
        path.rename(destination)
    (archive_dir / "README.md").write_text(
        "\n".join(
            [
                "# Stale RawRefs",
                "",
                "这些 RawRef 不再对应当前 `data/nas_memory.db` 的文档标题或 doc_key。",
                "文件已保留在这里，避免旧标题继续污染默认图谱和总索引。",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return len(stale_files)


def write_graph_config(vault_path: Path) -> None:
    obsidian_dir = vault_path / ".obsidian"
    obsidian_dir.mkdir(parents=True, exist_ok=True)
    (obsidian_dir / "graph.json").write_text(
        json.dumps(GRAPH_CONFIG, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def write_company_canvas(
    vault_path: Path,
    docs: list[dict[str, Any]],
    by_type: dict[str, list[dict[str, Any]]],
    generated_at: str,
) -> None:
    rawref_count = len(docs)
    p0_count = sum(
        1
        for doc in docs
        if doc["parser"] in {"docx", "md"} or doc["doc_type"] in {"SOP", "执行方案"}
    )
    top_types = ", ".join(
        f"{name} {len(items)}"
        for name, items in sorted(
            by_type.items(), key=lambda item: (-len(item[1]), item[0])
        )[:5]
    )
    canvas = {
        "nodes": [
            {
                "id": "root",
                "type": "text",
                "text": "# 公司知识地图\n\n人工排版的主地图，用来替代混乱的全局关系图谱。\n\n从这里看主干，再下钻到 RawRefs。",
                "x": 0,
                "y": 0,
                "width": 420,
                "height": 220,
                "color": "1",
            },
            {
                "id": "metrics",
                "type": "text",
                "text": f"## 当前全量 RawRef\n\n- RawRefs：{rawref_count}\n- P0 候选：{p0_count}\n- 源：NAS knowledge\n- 路径：/Users/dianchi/nas_kb\n- 生成：{generated_at}\n- 类型 Top5：{top_types}",
                "x": 0,
                "y": -320,
                "width": 460,
                "height": 240,
                "color": "6",
            },
            {
                "id": "product-customer",
                "type": "file",
                "file": "10_Index/产品客户图谱.md",
                "x": -620,
                "y": -120,
                "width": 340,
                "height": 170,
                "color": "2",
            },
            {
                "id": "product-line",
                "type": "file",
                "file": "10_Index/产品线.md",
                "x": -1040,
                "y": -290,
                "width": 300,
                "height": 130,
            },
            {
                "id": "customers",
                "type": "file",
                "file": "10_Index/客户.md",
                "x": -1040,
                "y": -110,
                "width": 300,
                "height": 130,
            },
            {
                "id": "wuling",
                "type": "file",
                "file": "10_Index/五菱.md",
                "x": -1400,
                "y": -380,
                "width": 260,
                "height": 120,
            },
            {
                "id": "baojun",
                "type": "file",
                "file": "10_Index/宝骏.md",
                "x": -1400,
                "y": -230,
                "width": 260,
                "height": 120,
            },
            {
                "id": "liuzhou",
                "type": "file",
                "file": "10_Index/柳汽.md",
                "x": -1400,
                "y": -80,
                "width": 260,
                "height": 120,
            },
            {
                "id": "dongfeng",
                "type": "file",
                "file": "10_Index/东风柳汽.md",
                "x": -1400,
                "y": 70,
                "width": 260,
                "height": 120,
            },
            {
                "id": "business",
                "type": "file",
                "file": "10_Index/业务主题图谱.md",
                "x": 620,
                "y": -120,
                "width": 340,
                "height": 170,
                "color": "3",
            },
            {
                "id": "projects",
                "type": "file",
                "file": "10_Index/项目.md",
                "x": 1040,
                "y": -300,
                "width": 300,
                "height": 130,
            },
            {
                "id": "market",
                "type": "file",
                "file": "10_Index/市场与品牌.md",
                "x": 1040,
                "y": -130,
                "width": 300,
                "height": 130,
            },
            {
                "id": "doctype",
                "type": "file",
                "file": "10_Index/文档类型图谱.md",
                "x": 1040,
                "y": 40,
                "width": 300,
                "height": 130,
            },
            {
                "id": "strategy",
                "type": "file",
                "file": "20_Bridges/传播策略.md",
                "x": 1400,
                "y": -260,
                "width": 260,
                "height": 110,
            },
            {
                "id": "review",
                "type": "file",
                "file": "20_Bridges/复盘结算.md",
                "x": 1400,
                "y": -120,
                "width": 260,
                "height": 110,
            },
            {
                "id": "copy",
                "type": "file",
                "file": "20_Bridges/文案素材.md",
                "x": 1400,
                "y": 20,
                "width": 260,
                "height": 110,
            },
            {
                "id": "people-map",
                "type": "file",
                "file": "10_Index/人员部门图谱.md",
                "x": 280,
                "y": 360,
                "width": 340,
                "height": 170,
                "color": "5",
            },
            {
                "id": "middle-office",
                "type": "file",
                "file": "20_Bridges/Departments/中台部门.md",
                "x": 700,
                "y": 160,
                "width": 300,
                "height": 130,
                "color": "5",
            },
            {
                "id": "customer-dept",
                "type": "file",
                "file": "20_Bridges/Departments/客户部.md",
                "x": 1060,
                "y": 250,
                "width": 300,
                "height": 130,
                "color": "5",
            },
            {
                "id": "planning",
                "type": "file",
                "file": "20_Bridges/Departments/策略部.md",
                "x": 700,
                "y": 330,
                "width": 300,
                "height": 130,
            },
            {
                "id": "collab",
                "type": "file",
                "file": "20_Bridges/Departments/执行部门.md",
                "x": 700,
                "y": 500,
                "width": 300,
                "height": 130,
            },
            {
                "id": "pending",
                "type": "file",
                "file": "20_Bridges/待人工确认.md",
                "x": -260,
                "y": 700,
                "width": 360,
                "height": 150,
                "color": "6",
            },
            {
                "id": "review-workbench",
                "type": "file",
                "file": "10_Index/复核工作台.md",
                "x": 260,
                "y": 700,
                "width": 360,
                "height": 150,
                "color": "6",
            },
            {
                "id": "p0",
                "type": "file",
                "file": "20_Bridges/Review/P0-优先复核.md",
                "x": 700,
                "y": 680,
                "width": 300,
                "height": 130,
            },
            {
                "id": "employee-review",
                "type": "file",
                "file": "20_Bridges/Review/员工确认记录.md",
                "x": 700,
                "y": 840,
                "width": 300,
                "height": 130,
                "color": "6",
            },
            {
                "id": "rule-confirmed",
                "type": "file",
                "file": "20_Bridges/规则确认.md",
                "x": 1040,
                "y": 680,
                "width": 300,
                "height": 130,
            },
            {
                "id": "rawrefs",
                "type": "text",
                "text": "## RawRefs 下钻层\n\n真实原始文档节点在 `00_RawRefs/`。\n\n默认不要把它们全放到全局图谱里；需要看某份文档时，打开该 RawRef 看局部图谱。",
                "x": 0,
                "y": 920,
                "width": 420,
                "height": 190,
                "color": "6",
            },
        ],
        "edges": [
            {
                "id": "e-root-metrics",
                "fromNode": "metrics",
                "fromSide": "bottom",
                "toNode": "root",
                "toSide": "top",
            },
            {
                "id": "e-root-product",
                "fromNode": "root",
                "fromSide": "left",
                "toNode": "product-customer",
                "toSide": "right",
            },
            {
                "id": "e-root-business",
                "fromNode": "root",
                "fromSide": "right",
                "toNode": "business",
                "toSide": "left",
            },
            {
                "id": "e-root-people",
                "fromNode": "root",
                "fromSide": "bottom",
                "toNode": "people-map",
                "toSide": "top",
            },
            {
                "id": "e-product-line",
                "fromNode": "product-customer",
                "fromSide": "left",
                "toNode": "product-line",
                "toSide": "right",
            },
            {
                "id": "e-product-customers",
                "fromNode": "product-customer",
                "fromSide": "left",
                "toNode": "customers",
                "toSide": "right",
            },
            {
                "id": "e-line-wuling",
                "fromNode": "product-line",
                "fromSide": "left",
                "toNode": "wuling",
                "toSide": "right",
            },
            {
                "id": "e-line-baojun",
                "fromNode": "product-line",
                "fromSide": "left",
                "toNode": "baojun",
                "toSide": "right",
            },
            {
                "id": "e-line-liuzhou",
                "fromNode": "product-line",
                "fromSide": "left",
                "toNode": "liuzhou",
                "toSide": "right",
            },
            {
                "id": "e-line-dongfeng",
                "fromNode": "customers",
                "fromSide": "left",
                "toNode": "dongfeng",
                "toSide": "right",
            },
            {
                "id": "e-business-projects",
                "fromNode": "business",
                "fromSide": "right",
                "toNode": "projects",
                "toSide": "left",
            },
            {
                "id": "e-business-market",
                "fromNode": "business",
                "fromSide": "right",
                "toNode": "market",
                "toSide": "left",
            },
            {
                "id": "e-business-doctype",
                "fromNode": "business",
                "fromSide": "right",
                "toNode": "doctype",
                "toSide": "left",
            },
            {
                "id": "e-doctype-strategy",
                "fromNode": "doctype",
                "fromSide": "right",
                "toNode": "strategy",
                "toSide": "left",
            },
            {
                "id": "e-doctype-review",
                "fromNode": "doctype",
                "fromSide": "right",
                "toNode": "review",
                "toSide": "left",
            },
            {
                "id": "e-doctype-copy",
                "fromNode": "doctype",
                "fromSide": "right",
                "toNode": "copy",
                "toSide": "left",
            },
            {
                "id": "e-people-middle",
                "fromNode": "people-map",
                "fromSide": "right",
                "toNode": "middle-office",
                "toSide": "left",
            },
            {
                "id": "e-middle-customer",
                "fromNode": "middle-office",
                "fromSide": "right",
                "toNode": "customer-dept",
                "toSide": "left",
            },
            {
                "id": "e-middle-planning",
                "fromNode": "middle-office",
                "fromSide": "bottom",
                "toNode": "planning",
                "toSide": "top",
            },
            {
                "id": "e-people-planning",
                "fromNode": "people-map",
                "fromSide": "right",
                "toNode": "planning",
                "toSide": "left",
            },
            {
                "id": "e-people-collab",
                "fromNode": "people-map",
                "fromSide": "right",
                "toNode": "collab",
                "toSide": "left",
            },
            {
                "id": "e-root-pending",
                "fromNode": "root",
                "fromSide": "bottom",
                "toNode": "pending",
                "toSide": "top",
            },
            {
                "id": "e-pending-review",
                "fromNode": "pending",
                "fromSide": "right",
                "toNode": "review-workbench",
                "toSide": "left",
            },
            {
                "id": "e-review-p0",
                "fromNode": "review-workbench",
                "fromSide": "right",
                "toNode": "p0",
                "toSide": "left",
            },
            {
                "id": "e-review-employee",
                "fromNode": "review-workbench",
                "fromSide": "right",
                "toNode": "employee-review",
                "toSide": "left",
            },
            {
                "id": "e-review-rule-confirmed",
                "fromNode": "review-workbench",
                "fromSide": "right",
                "toNode": "rule-confirmed",
                "toSide": "left",
            },
            {
                "id": "e-pending-rawrefs",
                "fromNode": "review-workbench",
                "fromSide": "bottom",
                "toNode": "rawrefs",
                "toSide": "top",
            },
        ],
    }
    (vault_path / "10_Index" / "公司知识地图.canvas").write_text(
        json.dumps(canvas, ensure_ascii=False, indent="\t") + "\n",
        encoding="utf-8",
    )


def generate(db_path: Path, vault_path: Path) -> dict[str, int]:
    raw_refs = vault_path / "00_RawRefs"
    indexes = vault_path / "10_Index"
    bridges = vault_path / "20_Bridges"
    reports = vault_path / "30_Reports"
    project_bridges = bridges / "Projects"
    people_bridges = bridges / "People"
    department_bridges = bridges / "Departments"
    entity_bridges = bridges / "Entities"
    review_bridges = bridges / "Review"
    for directory in (
        raw_refs,
        indexes,
        bridges,
        project_bridges,
        people_bridges,
        department_bridges,
        entity_bridges,
        review_bridges,
        reports,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    clear_generated_markdown(people_bridges)
    clear_generated_markdown(department_bridges)
    clear_generated_markdown(entity_bridges)
    clear_generated_markdown(project_bridges)
    org_model = load_org_model()
    known_people_names = org_people_names(org_model)
    known_departments = set((org_model.get("departments") or {}).keys())
    entity_taxonomy = load_entity_taxonomy()
    project_aliases = load_project_normalization()

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = query_documents(conn)
        chunks = chunk_stats(conn)
        title_counts = Counter(str(row["title"] or row["doc_key"]) for row in rows)
        docs: list[dict[str, Any]] = []
        expected_raw_ref_files: set[str] = set()
        for row in rows:
            base_title = str(row["title"] or row["doc_key"])
            title = base_title
            if title_counts[base_title] > 1:
                title = f"{base_title}-{str(row['doc_key'])[:8]}"
            filename = sanitize_filename(title, str(row["doc_key"])) + ".md"
            expected_raw_ref_files.add(filename)
            link_title = Path(filename).stem
            chunk_count = chunks.get(row["doc_key"], 0)
            preview = first_chunk_preview(conn, row["doc_key"])
            (raw_refs / filename).write_text(
                render_raw_ref(
                    row,
                    link_title,
                    chunk_count,
                    preview,
                    org_model,
                    project_aliases,
                ),
                encoding="utf-8",
            )
            raw_project_name = str(row["project_name"] or "").strip()
            project_name = normalize_project_name(raw_project_name, project_aliases)
            private_technical = is_private_technical_text(
                row["title"],
                row["rel_path"],
                row["doc_type"],
                raw_project_name,
            )
            people = load_people(row["participants_json"])
            departments = normalize_departments(
                load_json_array(row["departments_json"])
                + [person["department"] for person in people if person["department"]],
                org_model,
            )
            docs.append(
                {
                    "link_title": link_title,
                    "title": row["title"],
                    "summary": row["summary"],
                    "rel_path": row["rel_path"],
                    "doc_type": row["doc_type"],
                    "project_name": project_name,
                    "raw_project_name": raw_project_name,
                    "owner": row["owner"],
                    "departments": departments,
                    "people": [person["name"] for person in people],
                    "review_status": row["review_status"],
                    "parser": row["parser"],
                    "private_technical": private_technical,
                }
            )
    finally:
        conn.close()

    stale_raw_refs = archive_stale_raw_refs(raw_refs, expected_raw_ref_files)
    generated_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    review_records = load_jsonl(DEFAULT_REVIEW_CONFIRMATIONS)
    company_docs = [doc for doc in docs if not doc.get("private_technical")]
    (indexes / "公司知识库总索引.md").write_text(
        render_total_index(company_docs, generated_at),
        encoding="utf-8",
    )
    (indexes / "知识地图.md").write_text(
        render_clean_knowledge_map(company_docs, generated_at),
        encoding="utf-8",
    )
    (indexes / "来源文档.md").write_text(
        render_source_docs_index(company_docs, generated_at),
        encoding="utf-8",
    )
    (bridges / "待人工确认.md").write_text(
        render_review_bridge(company_docs, generated_at),
        encoding="utf-8",
    )
    (bridges / "规则确认.md").write_text(
        render_rule_confirmed_bridge(company_docs, generated_at),
        encoding="utf-8",
    )
    (indexes / "复核工作台.md").write_text(
        render_review_workbench(company_docs, generated_at),
        encoding="utf-8",
    )
    (review_bridges / "P0-优先复核.md").write_text(
        render_p0_review_bridge(company_docs, generated_at),
        encoding="utf-8",
    )
    (review_bridges / "员工确认记录.md").write_text(
        render_employee_review_confirmations(review_records, generated_at),
        encoding="utf-8",
    )

    by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_project: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_person: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_department: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for doc in company_docs:
        by_type[doc["doc_type"] or "待确认"].append(doc)
        if doc["project_name"]:
            by_project[doc["project_name"]].append(doc)
        if doc["owner"]:
            by_person[doc["owner"]].append(doc)
        for person in doc["people"]:
            by_person[person].append(doc)
        for department in doc["departments"]:
            by_department[department].append(doc)
    entity_matches = build_entity_matches(company_docs, entity_taxonomy)
    for entity_type, entities in entity_matches.items():
        for entity_name, entity_docs in entities.items():
            filename = (
                sanitize_filename(entity_link_name(entity_name), entity_name) + ".md"
            )
            (entity_bridges / filename).write_text(
                render_entity_taxonomy_bridge(
                    entity_type, entity_name, entity_docs, generated_at
                ),
                encoding="utf-8",
            )
    for doc_type, type_docs in by_type.items():
        filename = sanitize_filename(doc_type, "待确认") + ".md"
        (bridges / filename).write_text(
            render_doc_type_bridge(doc_type, type_docs, generated_at),
            encoding="utf-8",
        )
    for project_name, project_docs in by_project.items():
        filename = sanitize_filename(project_name, "未命名项目") + ".md"
        (project_bridges / filename).write_text(
            render_entity_bridge("项目", project_name, project_docs, generated_at),
            encoding="utf-8",
        )
    for person_name, person_docs in by_person.items():
        if person_name not in known_people_names:
            continue
        filename = sanitize_filename(person_name, "未命名人员") + ".md"
        (people_bridges / filename).write_text(
            render_entity_bridge("人员", person_name, person_docs, generated_at),
            encoding="utf-8",
        )
    for person_name in sorted((org_model.get("people") or {}).keys()):
        if person_name not in by_person:
            filename = sanitize_filename(person_name, "未命名人员") + ".md"
            (people_bridges / filename).write_text(
                render_entity_bridge("人员", person_name, [], generated_at),
                encoding="utf-8",
            )
    for department_name, department_docs in by_department.items():
        if department_name not in known_departments:
            continue
        filename = sanitize_filename(department_name, "未命名部门") + ".md"
        (department_bridges / filename).write_text(
            render_entity_bridge(
                "部门", department_name, department_docs, generated_at
            ),
            encoding="utf-8",
        )
    for department_name in sorted((org_model.get("departments") or {}).keys()):
        if department_name not in by_department:
            filename = sanitize_filename(department_name, "未命名部门") + ".md"
            (department_bridges / filename).write_text(
                render_entity_bridge("部门", department_name, [], generated_at),
                encoding="utf-8",
            )

    (bridges / "知识图谱入口.md").write_text(
        render_graph_entry(
            company_docs, by_type, by_project, by_person, by_department, generated_at
        ),
        encoding="utf-8",
    )
    (indexes / "产品客户图谱.md").write_text(
        render_focus_map(
            "产品客户图谱",
            "只保留客户、品牌和产品线主干；实体桥接页再下钻到 RawRef。",
            [
                (
                    "产品线",
                    [
                        entity_link_name(name)
                        for name in (entity_matches.get("产品线") or {})
                    ],
                ),
                (
                    "车型与品牌资产",
                    [
                        entity_link_name(name)
                        for name in (entity_matches.get("车型与品牌资产") or {})
                    ],
                ),
                ("入口", ["客户", "市场与品牌", "产品线"]),
            ],
            generated_at,
        ),
        encoding="utf-8",
    )
    (indexes / "业务主题图谱.md").write_text(
        render_focus_map(
            "业务主题图谱",
            "按已清洗业务实体和项目入口组织，不把项目明细直接塞进默认全局图谱。",
            [
                (
                    "已确认业务主题",
                    [
                        entity_link_name(name)
                        for name in (entity_matches.get("业务主题") or {})
                    ],
                ),
                ("主线入口", ["项目", "市场与品牌", "客户", "复核工作台"]),
                ("文档类型 Top", top_links(by_type, 12)),
                ("项目 Top", top_links(by_project, 20)),
            ],
            generated_at,
        ),
        encoding="utf-8",
    )
    (indexes / "人员部门图谱.md").write_text(
        render_personnel_department_map(
            org_model,
            by_person,
            by_department,
            generated_at,
        ),
        encoding="utf-8",
    )
    (indexes / "文档类型图谱.md").write_text(
        render_focus_map(
            "文档类型图谱",
            "按文档类型进入桥接页，再下钻到 RawRef。",
            [("类型", top_links(by_type, 30))],
            generated_at,
        ),
        encoding="utf-8",
    )
    (indexes / "项目.md").write_text(
        render_link_index(
            "项目",
            "当前由 NAS memory 索引自动识别出的项目节点。",
            list(by_project),
            generated_at,
        ),
        encoding="utf-8",
    )
    (indexes / "市场与品牌.md").write_text(
        render_link_index(
            "市场与品牌",
            "与品牌传播、营销策划、活动方案、礼品、账号运营和视觉素材有关的节点。",
            docs_matching(
                company_docs,
                [
                    "五菱",
                    "柳汽",
                    "东风",
                    "品牌",
                    "传播",
                    "礼品",
                    "活动",
                    "菱听",
                    "账号",
                    "丝巾",
                ],
            ),
            generated_at,
        ),
        encoding="utf-8",
    )
    (indexes / "客户.md").write_text(
        render_link_index(
            "客户",
            "从当前样本中能稳定识别出的客户或品牌主体。",
            [entity_link_name(name) for name in (entity_matches.get("产品线") or {})],
            generated_at,
        ),
        encoding="utf-8",
    )
    (indexes / "产品线.md").write_text(
        render_link_index(
            "产品线",
            "从当前样本中能稳定识别出的产品线和品牌线。",
            [entity_link_name(name) for name in (entity_matches.get("产品线") or {})],
            generated_at,
        ),
        encoding="utf-8",
    )
    for entity_name, patterns in {
        "五菱": ["五菱", "宏光", "缤果", "星光"],
        "柳汽": ["柳汽"],
        "东风柳汽": ["东风柳汽", "东风风行"],
        "华境": ["华境"],
        "宝骏": ["宝骏"],
    }.items():
        (indexes / f"{sanitize_filename(entity_name, entity_name)}.md").write_text(
            render_link_index(
                entity_name,
                "由文件名和路径关键词自动聚合的主体页，需人工复核。",
                docs_matching(company_docs, patterns),
                generated_at,
            ),
            encoding="utf-8",
        )

    (reports / "实体关系清洗确认报告.md").write_text(
        render_entity_relation_report(entity_matches, entity_taxonomy, generated_at),
        encoding="utf-8",
    )

    (reports / "第二阶段小闭环验证报告.md").write_text(
        "\n".join(
            [
                "# 第二阶段小闭环验证报告",
                "",
                f"生成时间：`{generated_at}`",
                "",
                "## 范围",
                "",
                "- 源数据：`data/nas_memory.db`",
                "- NAS 原始文件：只读，不移动、不删除、不覆盖",
                "- Obsidian 输出：`00_RawRefs`、`10_Index`、`20_Bridges`",
                "",
                "## 结果",
                "",
                f"- 生成 RawRef：{len(docs)}",
                f"- 公司图谱文档：{len(company_docs)}",
                f"- 技术资料隔离：{len(docs) - len(company_docs)}",
                f"- 生成文档类型桥接页：{len(by_type)}",
                f"- 生成项目桥接页：{len(by_project)}",
                f"- 生成人员桥接页：{len(known_people_names)}",
                f"- 生成部门桥接页：{len(known_departments)}",
                f"- 生成实体桥接页：{sum(len(items) for items in entity_matches.values())}",
                "- 生成待人工确认桥接页：1",
                f"- 规则确认文档：{sum(1 for doc in company_docs if doc['review_status'] == 'rule_confirmed')}",
                f"- 员工确认记录：{len(review_records)}",
                f"- 归档旧 RawRef：{stale_raw_refs}",
                "- 默认全局图谱过滤：`path:10_Index`",
                "- 干净入口：[[知识地图]]、[[公司知识地图.canvas]]",
                "",
                "## 下一步",
                "",
                "1. 在 Obsidian 中检查 RawRef 是否能回到原始 `source_path`。",
                "2. 人工确认 `doc_type`、`project_name`、`owner`。",
                "3. 从 [[复核工作台]] 选择 P0/P1/P2 批次做人工确认。",
                "4. 查看 [[员工确认记录]]，把对话中的顺手确认转成正式复核结论。",
                "",
                "## 2026-06-02 补充",
                "",
                "NAS 主知识库同步闭环已完成一次修复与复扫，详见 [[NAS知识库同步闭环修复报告]]。",
                "",
            ]
        ),
        encoding="utf-8",
    )
    write_graph_config(vault_path)
    write_company_canvas(vault_path, company_docs, by_type, generated_at)

    return {
        "raw_refs": len(docs),
        "company_graph_docs": len(company_docs),
        "private_technical_refs": len(docs) - len(company_docs),
        "doc_type_bridges": len(by_type),
        "project_bridges": len(by_project),
        "people_bridges": len(known_people_names),
        "department_bridges": len(known_departments),
        "entity_bridges": sum(len(items) for items in entity_matches.values()),
        "employee_review_records": len(review_records),
        "stale_raw_refs": stale_raw_refs,
        "reports": 2,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--vault", type=Path, default=DEFAULT_VAULT)
    args = parser.parse_args()
    stats = generate(args.db, args.vault)
    print(json.dumps(stats, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
