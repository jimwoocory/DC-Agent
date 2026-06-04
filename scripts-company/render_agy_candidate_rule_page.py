#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "nas_memory.db"
VAULT = ROOT / "ObsidianVault"
DEFAULT_OUTPUT = VAULT / "20_Bridges" / "Review" / "agy候选规则.md"


def sanitize_filename(value: str, fallback: str) -> str:
    name = re.sub(r"[\\/:*?\"<>|#^[\]]+", " ", value).strip()
    name = re.sub(r"\s+", " ", name)
    return name[:120].strip() or fallback


def load_title_counts() -> dict[str, int]:
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            "select coalesce(title, doc_key) as title, count(*) from documents group by coalesce(title, doc_key)"
        ).fetchall()
    finally:
        conn.close()
    return {str(title): int(count) for title, count in rows}


def load_queue_status() -> dict[str, str]:
    conn = sqlite3.connect(DB_PATH)
    try:
        exists = conn.execute(
            "select 1 from sqlite_master where type = 'table' and name = 'agy_candidate_rules'"
        ).fetchone()
        if not exists:
            return {}
        rows = conn.execute(
            "select doc_key, status from agy_candidate_rules"
        ).fetchall()
    finally:
        conn.close()
    return {str(doc_key): str(status) for doc_key, status in rows}


def raw_ref_title(title: str, doc_key: str, title_counts: dict[str, int]) -> str:
    base = title or doc_key
    if title_counts.get(base, 0) > 1:
        return sanitize_filename(f"{base}-{doc_key[:8]}", doc_key)
    return sanitize_filename(base, doc_key)


def join_values(values: list[Any]) -> str:
    return (
        "、".join(str(value).strip() for value in values if str(value).strip())
        or "待确认"
    )


def verdict_summary(verdict: dict[str, Any]) -> str:
    kind = str(verdict.get("kind") or "")
    if kind == "project_tracker_row_owner_match":
        row = verdict.get("tracker_row") or {}
        return (
            f"`{kind}`；score `{verdict.get('match_score')}`；"
            f"总表第 `{row.get('row')}` 行 `{row.get('plan_name')}`"
        )
    if kind == "high_confidence_known_owner_department":
        return f"`{kind}`；{join_values(verdict.get('evidence') or [])}"
    return kind or "待确认"


def render_table(
    items: list[dict[str, Any]],
    mode: str,
    title_counts: dict[str, int],
    queue_status: dict[str, str],
) -> list[str]:
    if not items:
        return ["- 无", ""]
    if mode == "rejected":
        lines = [
            "| 文档 | agy 建议 | 队列 | 拦截原因 | 风险说明 |",
            "|---|---|---|---|---|",
        ]
        for item in items:
            link = raw_ref_title(
                str(item.get("title") or ""),
                str(item.get("doc_key") or ""),
                title_counts,
            )
            suggestion = f"{item.get('agy_owner') or '待确认'} / {join_values(item.get('agy_departments') or [])}"
            status = queue_status.get(str(item.get("doc_key") or ""), "未入队")
            lines.append(
                f"| [[{link}]] | {suggestion} | {status} | `{item.get('reason') or ''}` | {item.get('agy_risk_reason') or '无'} |"
            )
        lines.append("")
        return lines

    lines = [
        "| 文档 | agy 建议 | 队列 | 本地证据 | 处理建议 |",
        "|---|---|---|---|---|",
    ]
    for item in items:
        doc_key = str(item.get("doc_key") or "")
        link = raw_ref_title(
            str(item.get("title") or ""), str(item.get("doc_key") or ""), title_counts
        )
        verdict = item.get("local_verdict") or {}
        local_owner = verdict.get("owner") or item.get("agy_owner") or "待确认"
        local_departments = (
            verdict.get("departments") or item.get("agy_departments") or []
        )
        suggestion = f"{local_owner} / {join_values(local_departments)}"
        status = queue_status.get(doc_key, "未入队")
        advice = (
            "可进入 agy_candidate_rule，抽查后再 apply"
            if mode == "accepted"
            else "本地规则可补充，建议人工抽查"
        )
        lines.append(
            f"| [[{link}]] | {suggestion} | {status} | {verdict_summary(verdict)} | {advice} |"
        )
    lines.append("")
    return lines


def render(report: dict[str, Any]) -> str:
    title_counts = load_title_counts()
    queue_status = load_queue_status()
    lines = [
        "# agy候选规则",
        "",
        f"生成时间：`{report.get('generated_at')}`",
        f"来源：`{report.get('source')}`",
        "",
        "## 口径",
        "",
        "- 本页只记录 agy 候选与本地规则交叉验证结果。",
        "- 本页不代表已经写入 `rule_confirmed`。",
        "- `agy + 本地共同通过` 可进入 `agy_candidate_rule` 缓冲队列，抽查后再考虑 apply。",
        "- `本地拦截` 说明 agy 给了候选，但本地没有项目总表、人员部门或高置信证据支撑。",
        "",
        "## 概览",
        "",
        f"- 输入条数：{report.get('input_items')}",
        f"- agy 状态分布：`{json.dumps(report.get('agy_status_counts') or {}, ensure_ascii=False)}`",
        f"- agy + 本地共同通过：{report.get('accepted_count')}",
        f"- 本地规则发现但 agy 未建议：{report.get('local_only_count')}",
        f"- 本地拦截：{report.get('rejected_count')}",
        f"- 仍需人工：{report.get('needs_human_count')}",
        "- 抽查报告：[[agy候选规则抽查报告]]",
        "",
        "## agy + 本地共同通过",
        "",
        *render_table(
            report.get("accepted") or [], "accepted", title_counts, queue_status
        ),
        "## 本地规则发现但 agy 未建议",
        "",
        *render_table(
            report.get("local_only") or [], "local_only", title_counts, queue_status
        ),
        "## 本地拦截",
        "",
        *render_table(
            report.get("rejected") or [], "rejected", title_counts, queue_status
        ),
        "## 下一步",
        "",
        "1. 先处理队列状态为 `audit_passed_remap_required` 的候选，并按公司正式组织架构重映射部门。",
        "2. 队列状态为 `hold_owner_not_in_company_org` 的候选暂不提升，等确认是否为历史人员、外协或旧项目负责人。",
        "3. apply 前备份数据库；apply 后重建 Obsidian：`generate_obsidian_refs.py` 与 `generate_review_workbench.py`。",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("local_validation_json", type=Path)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    report = json.loads(args.local_validation_json.read_text(encoding="utf-8"))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render(report), encoding="utf-8")
    print(
        json.dumps(
            {"output": str(args.output), "accepted": report.get("accepted_count", 0)},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
