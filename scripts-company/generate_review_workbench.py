#!/usr/bin/env python3
"""Generate an Obsidian review workbench for NAS memory documents."""

from __future__ import annotations

import argparse
import importlib.util
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
TABLE_OWNER_RULE_SCRIPT = (
    DC_ROOT / "scripts-company" / "apply_table_owner_confirmations.py"
)


def sanitize_filename(value: str, fallback: str) -> str:
    name = re.sub(r"[\\/:*?\"<>|#^[\\]]+", " ", value).strip()
    name = re.sub(r"\s+", " ", name)
    return name[:120].strip() or fallback


def raw_ref_title(title: str, doc_key: str, title_counts: Counter[str]) -> str:
    base = title or doc_key
    if title_counts[base] > 1:
        return sanitize_filename(f"{base}-{doc_key[:8]}", doc_key)
    return sanitize_filename(base, doc_key)


def priority_bucket(row: sqlite3.Row) -> tuple[str, str]:
    parser = row["parser"]
    doc_type = row["doc_type"] or ""
    rel_path = row["rel_path"] or ""
    title = row["title"] or ""
    chunk_count = int(row["chunk_count"] or 0)

    if parser in {"docx", "md"}:
        return "P0-优先复核", "文本型文档，最适合作为问答知识库种子"
    if doc_type in {"SOP", "执行方案"}:
        return "P0-优先复核", "SOP/执行方案，适合先验证问答"
    if doc_type in {"传播策略", "排期分工"} and chunk_count >= 2:
        return "P1-业务复核", "传播/排期类材料，适合业务主题归类"
    if doc_type in {"复盘结算", "预算报价"}:
        return "P1-业务复核", "复盘/预算类材料，适合复核后进入案例库"
    if "PPT版本" in rel_path or parser == "pptx":
        return "P2-附件与重复", "PPT/附件类材料，优先排重和版本确认"
    if "最新版方案" in rel_path and re.search(r"\d{4}_", title):
        return "P2-附件与重复", "编号附件，需确认是否已有 PPT/PDF 重复版本"
    return "P2-附件与重复", "低优先级或需人工判断"


def query_docs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        select
            d.doc_key,
            d.rel_path,
            d.source_path,
            d.title,
            d.doc_type,
            d.parser,
            d.project_name,
            d.owner,
            d.review_status,
            d.file_size,
            count(c.chunk_id) as chunk_count
        from documents d
        left join chunks c on c.doc_key = d.doc_key
        group by d.doc_key
        order by
            case
                when d.parser = 'docx' then 0
                when d.parser = 'md' then 1
                when d.doc_type in ('SOP', '执行方案') then 2
                when d.doc_type in ('传播策略', '排期分工') then 3
                when d.doc_type in ('复盘结算', '预算报价') then 4
                else 5
            end,
            chunk_count desc,
            d.title asc
        """
    ).fetchall()


def render_table(
    rows: list[dict[str, Any]],
    limit: int | None = None,
    status_label: str = "[ ]",
) -> list[str]:
    lines = [
        "| 状态 | 文档 | 类型 | 解析 | chunks | 建议 |",
        "|---|---|---|---:|---:|---|",
    ]
    selected = rows if limit is None else rows[:limit]
    for row in selected:
        lines.append(
            f"| {status_label} | [[{row['link_title']}]] | {row['doc_type'] or '待确认'} | {row['parser']} | {row['chunk_count']} | {row['reason']} |"
        )
    if limit is not None and len(rows) > limit:
        lines.append(f"|  | 还有 {len(rows) - limit} 条 |  |  |  | 到批次页查看 |")
    return lines


def render_rule_confirmed_page(rows: list[dict[str, Any]], generated_at: str) -> str:
    lines = [
        "# P3-规则确认",
        "",
        f"生成时间：`{generated_at}`",
        "",
        "## 口径",
        "",
        "- 这些文档不是人工确认，而是满足保守规则后从人工待办队列移出。",
        "- 当前规则一：`confidence >= 0.95`，负责人、部门、文档类型、项目名齐全，且负责人/部门在人工规则表中可识别。",
        "- 当前规则二：项目总表行级证据命中，匹配分数达到阈值，且负责人可由人工规则表解析部门。",
        "- 仍可从 RawRef 下钻检查 `metadata.rule_confirmation` 的确认依据。",
        "",
        "## 队列",
        "",
        *render_table(rows, status_label="规则确认"),
        "",
    ]
    return "\n".join(lines)


def load_table_owner_rule_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "table_owner_rule", TABLE_OWNER_RULE_SCRIPT
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(
            f"Cannot load table owner rule script: {TABLE_OWNER_RULE_SCRIPT}"
        )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def query_p4_people_rule_candidates(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    module = load_table_owner_rule_module()
    overrides = module.load_overrides(module.DEFAULT_OVERRIDES)
    person_departments = module.people_department_map(overrides)
    module.add_db_people(conn, person_departments)
    tracker_rows = module.parse_tracker_rows(conn)

    candidates: dict[str, dict[str, Any]] = {}
    for min_confidence, min_score in ((0.88, 0.88), (0.78, 1.0)):
        _, blocked = module.query_candidates(
            conn,
            tracker_rows,
            person_departments,
            min_confidence,
            min_score,
        )
        for item in blocked:
            doc_key = str(item["row"]["doc_key"])
            item = dict(item)
            item["missing_owners"] = [
                str(owner)
                for owner in item["owners"]
                if not person_departments.get(str(owner))
            ]
            candidates[doc_key] = item
    return list(candidates.values())


def render_p4_page(
    rows: list[dict[str, Any]],
    generated_at: str,
    title_counts: Counter[str],
) -> str:
    lines = [
        "# P4-待补人员规则",
        "",
        f"生成时间：`{generated_at}`",
        "",
        "## 口径",
        "",
        "这些文档已经有项目总表行级证据，但负责人或部门还没有进入 `nas_memory_overrides.json` 或本地 `people` 表，所以暂不进入 `rule_confirmed`。",
        "",
    ]
    if not rows:
        lines.extend(
            [
                "## 当前缺口",
                "",
                "当前没有待补人员/部门规则的候选。",
                "",
                "## 下一步",
                "",
                "后续 NAS/飞书同步新增文档后，重新运行复核工作台生成器；如果出现新候选，本页会自动列出。",
                "",
            ]
        )
        return "\n".join(lines)

    grouped: Counter[str] = Counter()
    serialised: list[dict[str, Any]] = []
    for item in rows:
        row = item["row"]
        owners = [str(owner) for owner in item["owners"]]
        known_departments = [str(department) for department in item["departments"]]
        missing_owners = [str(owner) for owner in item.get("missing_owners", [])]
        owner_text = "、".join(owners)
        missing_text = "、".join(missing_owners) if missing_owners else owner_text
        grouped[missing_text] += 1
        serialised.append(
            {
                "link_title": raw_ref_title(row["title"], row["doc_key"], title_counts),
                "owner_text": owner_text,
                "missing_text": missing_text,
                "known_departments": "、".join(known_departments),
                "row_number": item["tracker_row"]["row"],
                "plan_name": item["tracker_row"]["plan_name"],
                "score": float(item["score"] or 0),
            }
        )

    lines.extend(
        [
            "## 当前缺口",
            "",
            "| 候选负责人 | 数量 | 缺口 |",
            "|---|---:|---|",
        ]
    )
    for owner_text, count in grouped.most_common():
        lines.append(f"| {owner_text} | {count} | 缺部门归属 |")

    lines.extend(
        [
            "",
            "## 候选",
            "",
            "| 状态 | 文档 | 候选负责人 | 缺口 | 证据 |",
            "|---|---|---|---|---|",
        ]
    )
    for item in serialised:
        known = (
            f"；已知部门：{item['known_departments']}"
            if item["known_departments"]
            else ""
        )
        lines.append(
            f"| 待补人员规则 | [[{item['link_title']}]] | {item['owner_text']} | 缺 {item['missing_text']} 部门归属{known} | 项目总表第 {item['row_number']} 行：{item['plan_name']}；匹配 {item['score']:.4f} |"
        )

    lines.extend(
        [
            "",
            "## 下一步",
            "",
            "确认候选人的部门归属后，先更新 `data/config/nas_memory_overrides.json` 或本地人员表，再重新执行规则确认脚本。",
            "",
        ]
    )
    return "\n".join(lines)


def render_batch_page(name: str, rows: list[dict[str, Any]], generated_at: str) -> str:
    lines = [
        f"# {name}",
        "",
        f"生成时间：`{generated_at}`",
        "",
        "## 复核规则",
        "",
        "- 勾选前先打开 RawRef，确认 `source_path`、摘要、正文预览是否可信。",
        "- 能直接进入问答测试的，标记为“通过”。",
        "- 归属不清、重复、版本不清的，保留在本批次，不进入问答测试。",
        "",
        "## 队列",
        "",
        *render_table(rows),
        "",
    ]
    return "\n".join(lines)


def render_workbench(
    docs: list[dict[str, Any]],
    buckets: dict[str, list[dict[str, Any]]],
    p4_count: int,
    generated_at: str,
) -> str:
    by_type = Counter(doc["doc_type"] or "待确认" for doc in docs)
    by_parser = Counter(doc["parser"] or "unknown" for doc in docs)
    need_review = sum(1 for doc in docs if doc["review_status"] == "need_review")
    rule_confirmed = sum(1 for doc in docs if doc["review_status"] == "rule_confirmed")
    confirmed = sum(1 for doc in docs if doc["review_status"] == "confirmed")

    lines = [
        "# 复核工作台",
        "",
        f"生成时间：`{generated_at}`",
        "",
        "## 当前状态",
        "",
        f"- 已索引文档：{len(docs)}",
        f"- 待复核：{need_review}",
        f"- 规则确认：{rule_confirmed}",
        f"- 已确认：{confirmed}",
        f"- 解析器分布：{', '.join(f'{k} {v}' for k, v in sorted(by_parser.items()))}",
        f"- 类型分布：{', '.join(f'{k} {v}' for k, v in by_type.most_common())}",
        "",
        "## 推荐顺序",
        "",
        "1. [[P0-优先复核]]：先确认文本型、SOP、执行方案，作为问答测试种子。",
        "2. [[P1-业务复核]]：确认传播策略、排期、复盘、预算。",
        "3. [[P2-附件与重复]]：处理 PDF/PPTX 重复版本、附件型材料和低优先级资料。",
        "4. [[P3-规则确认]]：查看已被保守规则移出人工队列的文档。",
        f"5. [[P4-待补人员规则]]：查看有证据但缺人员/部门规则的候选（当前 {p4_count} 条）。",
        "",
        "## P0 预览",
        "",
        "第一轮问答测试种子：[[P0-问答测试种子]]",
        "",
        "P0 问答验证：[[P0问答验证报告]]",
        "",
        "P0 端到端注入验证：[[P0端到端问答注入验证报告]]",
        "",
        "P0 真实回答 Smoke：[[P0真实回答Smoke报告]]",
        "",
        "组织归属校正：[[组织归属校正记录]]",
        "",
        "归属判定规则：[[组织归属判定规则]]",
        "",
        *render_table(buckets.get("P0-优先复核", []), limit=20),
        "",
        "## 操作口径",
        "",
        "- 本工作台不修改 NAS 原始文件。",
        "- 本工作台不写入 AstrBot KB。",
        "- `规则确认` 只代表机器规则通过，不等同于人工确认。",
        "- 复核通过后，再选择 10-20 个文档做问答效果验证。",
        "",
    ]
    return "\n".join(lines)


def render_summary(
    docs: list[dict[str, Any]],
    buckets: dict[str, list[dict[str, Any]]],
    p4_count: int,
    generated_at: str,
) -> str:
    rule_confirmed = sum(1 for doc in docs if doc["review_status"] == "rule_confirmed")
    lines = [
        "# 人工复核清单",
        "",
        f"生成时间：`{generated_at}`",
        "",
        "## 批次统计",
        "",
        "| 批次 | 数量 | 目标 |",
        "|---|---:|---|",
        f"| P0-优先复核 | {len(buckets.get('P0-优先复核', []))} | 问答测试种子 |",
        f"| P1-业务复核 | {len(buckets.get('P1-业务复核', []))} | 业务归类和案例库 |",
        f"| P2-附件与重复 | {len(buckets.get('P2-附件与重复', []))} | 去重、版本确认、低优先级 |",
        f"| P3-规则确认 | {rule_confirmed} | 规则移出人工队列，保留追溯 |",
        f"| P4-待补人员规则 | {p4_count} | 补人员/部门规则后再进入规则确认 |",
        "",
        "## 下一步建议",
        "",
        "先完成 P0 的 10-20 个文档复核，再进入 AstrBot KB 问答测试。不要在 P0 未确认前扩大导入。",
        "",
    ]
    return "\n".join(lines)


def generate(db_path: Path, vault_path: Path) -> dict[str, int]:
    review_dir = vault_path / "20_Bridges" / "Review"
    index_dir = vault_path / "10_Index"
    reports_dir = vault_path / "30_Reports"
    review_dir.mkdir(parents=True, exist_ok=True)
    index_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = query_docs(conn)
        p4_people_rule_candidates = query_p4_people_rule_candidates(conn)
    finally:
        conn.close()

    title_counts = Counter(str(row["title"] or row["doc_key"]) for row in rows)
    docs: list[dict[str, Any]] = []
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    rule_confirmed_docs: list[dict[str, Any]] = []
    for row in rows:
        bucket, reason = priority_bucket(row)
        doc = {
            "doc_key": row["doc_key"],
            "rel_path": row["rel_path"],
            "source_path": row["source_path"],
            "title": row["title"],
            "link_title": raw_ref_title(row["title"], row["doc_key"], title_counts),
            "doc_type": row["doc_type"],
            "parser": row["parser"],
            "project_name": row["project_name"],
            "owner": row["owner"],
            "review_status": row["review_status"],
            "file_size": int(row["file_size"] or 0),
            "chunk_count": int(row["chunk_count"] or 0),
            "reason": reason,
        }
        docs.append(doc)
        if doc["review_status"] == "rule_confirmed":
            doc["reason"] = "满足规则确认条件，保留追溯"
            rule_confirmed_docs.append(doc)
        elif doc["review_status"] == "need_review":
            buckets[bucket].append(doc)

    generated_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    for bucket_name in ("P0-优先复核", "P1-业务复核", "P2-附件与重复"):
        bucket_docs = buckets.get(bucket_name, [])
        (review_dir / f"{bucket_name}.md").write_text(
            render_batch_page(bucket_name, bucket_docs, generated_at),
            encoding="utf-8",
        )
    (review_dir / "P3-规则确认.md").write_text(
        render_rule_confirmed_page(rule_confirmed_docs, generated_at),
        encoding="utf-8",
    )
    (review_dir / "P4-待补人员规则.md").write_text(
        render_p4_page(p4_people_rule_candidates, generated_at, title_counts),
        encoding="utf-8",
    )

    (index_dir / "复核工作台.md").write_text(
        render_workbench(docs, buckets, len(p4_people_rule_candidates), generated_at),
        encoding="utf-8",
    )
    (reports_dir / "人工复核清单.md").write_text(
        render_summary(docs, buckets, len(p4_people_rule_candidates), generated_at),
        encoding="utf-8",
    )
    return {
        "docs": len(docs),
        "p0": len(buckets.get("P0-优先复核", [])),
        "p1": len(buckets.get("P1-业务复核", [])),
        "p2": len(buckets.get("P2-附件与重复", [])),
        "p3_rule_confirmed": len(rule_confirmed_docs),
        "p4_people_rules": len(p4_people_rule_candidates),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--vault", type=Path, default=DEFAULT_VAULT)
    args = parser.parse_args()
    print(json.dumps(generate(args.db, args.vault), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
