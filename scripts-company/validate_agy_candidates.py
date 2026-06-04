#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "nas_memory.db"
OUT_DIR = ROOT / "data" / "agy_review_candidates"
OVERRIDES_PATH = ROOT / "data" / "config" / "nas_memory_overrides.json"


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


table_rules = load_module(
    ROOT / "scripts-company" / "apply_table_owner_confirmations.py", "table_rules"
)
high_rules = load_module(
    ROOT / "scripts-company" / "apply_rule_confirmations.py", "high_rules"
)


def read_json(path: Path) -> dict[str, Any]:
    parsed = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return parsed


def unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        cleaned = str(value or "").strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            out.append(cleaned)
    return out


def load_doc(conn: sqlite3.Connection, doc_key: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        select
            doc_key,
            title,
            rel_path,
            project_id,
            project_name,
            doc_type,
            owner,
            departments_json,
            participants_json,
            confidence,
            review_status,
            metadata_json
        from documents
        where doc_key = ?
        """,
        (doc_key,),
    ).fetchone()


def high_confidence_verdict(
    row: sqlite3.Row, known_people: set[str], known_departments: set[str]
) -> dict[str, Any] | None:
    evidence = high_rules.candidate_reason(row, known_people, known_departments, 0.95)
    if evidence is None:
        return None
    return {
        "kind": "high_confidence_known_owner_department",
        "owner": str(row["owner"] or ""),
        "departments": high_rules.load_json_array(row["departments_json"]),
        "evidence": evidence,
    }


def tracker_verdict(
    row: sqlite3.Row,
    tracker_rows: list[dict[str, Any]],
    person_departments: dict[str, str],
    min_score: float,
) -> dict[str, Any] | None:
    doc_name = str(row["project_name"] or row["title"] or "")
    score, tracker_row = table_rules.best_tracker_match(doc_name, tracker_rows)
    if tracker_row is None or score < min_score:
        return None
    owners = list(tracker_row["owners"])
    departments = table_rules.departments_for_owners(owners, person_departments)
    if len(departments) != len(owners):
        return {
            "kind": "tracker_blocked_missing_department",
            "match_score": round(score, 4),
            "owners": owners,
            "departments": departments,
            "tracker_row": {
                "row": tracker_row["row"],
                "plan_name": tracker_row["plan_name"],
                "owner_text": tracker_row["owner_text"],
            },
        }
    return {
        "kind": "project_tracker_row_owner_match",
        "match_score": round(score, 4),
        "owner": "、".join(owners),
        "departments": departments,
        "tracker_row": {
            "row": tracker_row["row"],
            "plan_name": tracker_row["plan_name"],
            "owner_text": tracker_row["owner_text"],
        },
    }


def same_owner_dept(item: dict[str, Any], verdict: dict[str, Any]) -> bool:
    agy_owner = str(item.get("suggested_owner") or "").strip()
    verdict_owner = str(verdict.get("owner") or "").strip()
    if agy_owner and verdict_owner and agy_owner != verdict_owner:
        return False
    agy_departments = unique([str(x) for x in item.get("suggested_departments") or []])
    verdict_departments = unique([str(x) for x in verdict.get("departments") or []])
    if (
        agy_departments
        and verdict_departments
        and agy_departments != verdict_departments
    ):
        return False
    return True


def validate(path: Path, min_tracker_score: float) -> dict[str, Any]:
    data = read_json(path)
    items = data.get("items") or []
    if not isinstance(items, list):
        raise ValueError("items must be a list")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        overrides = table_rules.load_overrides(OVERRIDES_PATH)
        person_departments = table_rules.people_department_map(overrides)
        table_rules.add_db_people(conn, person_departments)
        known_people, known_departments = high_rules.known_people_and_departments(
            overrides
        )
        high_rules.add_db_people(conn, known_people, known_departments)
        tracker_rows = table_rules.parse_tracker_rows(conn)

        accepted: list[dict[str, Any]] = []
        local_only: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        needs_human: list[dict[str, Any]] = []
        missing: list[dict[str, Any]] = []

        for item in items:
            doc_key = str(item.get("doc_key") or "").strip()
            row = load_doc(conn, doc_key)
            if row is None:
                missing.append({"doc_key": doc_key, "title": item.get("title")})
                continue

            agy_status = str(item.get("candidate_status") or "")
            verdicts: list[dict[str, Any]] = []
            high = high_confidence_verdict(row, known_people, known_departments)
            if high:
                verdicts.append(high)
            tracker = tracker_verdict(
                row, tracker_rows, person_departments, min_tracker_score
            )
            if tracker:
                verdicts.append(tracker)

            valid_verdicts = [
                v
                for v in verdicts
                if not str(v.get("kind", "")).startswith("tracker_blocked")
            ]
            blocked_verdicts = [
                v
                for v in verdicts
                if str(v.get("kind", "")).startswith("tracker_blocked")
            ]

            base = {
                "doc_key": doc_key,
                "title": row["title"],
                "agy_status": agy_status,
                "agy_owner": item.get("suggested_owner") or "",
                "agy_departments": item.get("suggested_departments") or [],
                "agy_confidence": item.get("confidence"),
                "agy_evidence": item.get("evidence") or [],
                "agy_risk_reason": item.get("risk_reason") or "",
                "project_name": row["project_name"],
                "doc_type": row["doc_type"],
                "db_confidence": row["confidence"],
            }

            if valid_verdicts:
                matched = [v for v in valid_verdicts if same_owner_dept(item, v)]
                if matched and agy_status == "candidate_rule":
                    accepted.append({**base, "local_verdict": matched[0]})
                elif matched:
                    local_only.append({**base, "local_verdict": matched[0]})
                else:
                    rejected.append(
                        {
                            **base,
                            "reason": "agy_owner_or_department_differs_from_local_verdict",
                            "local_verdicts": valid_verdicts,
                        }
                    )
            elif agy_status == "candidate_rule":
                rejected.append(
                    {
                        **base,
                        "reason": "no_local_confirming_evidence",
                        "blocked_verdicts": blocked_verdicts,
                    }
                )
            else:
                needs_human.append({**base, "local_verdicts": verdicts})
    finally:
        conn.close()

    status_counts = Counter(
        str(item.get("candidate_status") or "")
        for item in items
        if isinstance(item, dict)
    )
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "generated_at": generated_at,
        "source": str(path),
        "batch_id": data.get("batch_id"),
        "min_tracker_score": min_tracker_score,
        "input_items": len(items),
        "agy_status_counts": dict(status_counts),
        "accepted_count": len(accepted),
        "local_only_count": len(local_only),
        "rejected_count": len(rejected),
        "needs_human_count": len(needs_human),
        "missing_count": len(missing),
        "accepted": accepted,
        "local_only": local_only,
        "rejected": rejected,
        "missing": missing,
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# agy 候选本地验证报告",
        "",
        f"生成时间：`{report['generated_at']}`",
        f"来源：`{report['source']}`",
        "",
        "## 概览",
        "",
        f"- 输入条数：{report['input_items']}",
        f"- agy 状态分布：`{json.dumps(report['agy_status_counts'], ensure_ascii=False)}`",
        f"- agy + 本地验证共同通过：{report['accepted_count']}",
        f"- 本地规则发现但 agy 未建议：{report['local_only_count']}",
        f"- 本地拦截：{report['rejected_count']}",
        f"- 仍需人工：{report['needs_human_count']}",
        f"- 数据库缺失：{report['missing_count']}",
        "",
        "## 本地验证通过",
        "",
    ]
    if report["accepted"]:
        lines.append("| 标题 | agy 建议 | 本地证据 |")
        lines.append("|---|---|---|")
        for item in report["accepted"]:
            verdict = item["local_verdict"]
            evidence = verdict["kind"]
            if verdict.get("match_score") is not None:
                evidence += f" score={verdict['match_score']}"
            lines.append(
                f"| {item['title']} | {item['agy_owner']} / {','.join(item['agy_departments'])} | {evidence} |"
            )
    else:
        lines.append("- 无")
    lines.extend(["", "## 本地拦截", ""])
    if report["rejected"]:
        lines.append("| 标题 | agy 建议 | 拦截原因 |")
        lines.append("|---|---|---|")
        for item in report["rejected"]:
            lines.append(
                f"| {item['title']} | {item['agy_owner']} / {','.join(item['agy_departments'])} | {item['reason']} |"
            )
    else:
        lines.append("- 无")
    lines.extend(["", "## 本地规则发现但 agy 未建议", ""])
    if report["local_only"]:
        lines.append("| 标题 | 本地证据 |")
        lines.append("|---|---|")
        for item in report["local_only"]:
            verdict = item["local_verdict"]
            evidence = verdict["kind"]
            if verdict.get("match_score") is not None:
                evidence += f" score={verdict['match_score']}"
            lines.append(f"| {item['title']} | {evidence} |")
    else:
        lines.append("- 无")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("agy_candidates", type=Path)
    parser.add_argument("--min-tracker-score", type=float, default=0.88)
    args = parser.parse_args()

    report = validate(args.agy_candidates, args.min_tracker_score)
    stem = args.agy_candidates.name.removesuffix(".agy-candidates.json")
    json_path = OUT_DIR / f"{stem}.local-validation.json"
    md_path = OUT_DIR / f"{stem}.local-validation.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    md_path.write_text(render_markdown(report), encoding="utf-8")
    print(
        json.dumps(
            {
                "input_items": report["input_items"],
                "agy_status_counts": report["agy_status_counts"],
                "accepted_count": report["accepted_count"],
                "local_only_count": report["local_only_count"],
                "rejected_count": report["rejected_count"],
                "needs_human_count": report["needs_human_count"],
                "missing_count": report["missing_count"],
                "json_path": str(json_path),
                "md_path": str(md_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
