#!/usr/bin/env python3
"""Confirm documents whose owner is backed by the planning tracker table."""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DC_ROOT = Path("/Users/dianchi/DC-Agent")
DEFAULT_DB = DC_ROOT / "data" / "nas_memory.db"
DEFAULT_OVERRIDES = DC_ROOT / "data" / "config" / "nas_memory_overrides.json"
RULE_STATUS = "rule_confirmed"
RULE_NAME = "project_tracker_row_owner_match"
TRACKER_TITLE = "策划部日常工作方案进度记录"


def load_json_array(value: str | None) -> list[Any]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def load_json_dict(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def load_overrides(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    parsed = json.loads(path.read_text(encoding="utf-8"))
    return parsed if isinstance(parsed, dict) else {}


def people_department_map(overrides: dict[str, Any]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    departments = overrides.get("departments") or {}
    if isinstance(departments, dict):
        for department_name, item in departments.items():
            if isinstance(item, dict):
                lead = str(item.get("lead") or "").strip()
                if lead and lead not in mapping:
                    mapping[lead] = str(department_name).strip()

    people = overrides.get("people") or {}
    if isinstance(people, dict):
        for person_name, item in people.items():
            name = str(person_name).strip()
            department = (
                str(item.get("department") or "").strip()
                if isinstance(item, dict)
                else ""
            )
            if name and department:
                mapping[name] = department
    return mapping


def add_db_people(conn: sqlite3.Connection, mapping: dict[str, str]) -> int:
    try:
        rows = conn.execute(
            """
            select name, department
            from people
            where coalesce(name, '') <> ''
              and coalesce(department, '') <> ''
            """
        ).fetchall()
    except sqlite3.OperationalError:
        return 0

    added = 0
    for row in rows:
        name = str(row["name"] or "").strip()
        department = str(row["department"] or "").strip()
        if not name or not department:
            continue
        if name not in mapping:
            added += 1
        mapping[name] = department
    return added


def unique_values(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        cleaned = value.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        result.append(cleaned)
    return result


def split_people(value: str) -> list[str]:
    if not value or value.strip() == "/":
        return []
    parts = re.split(r"[、,，/]+", value)
    return unique_values([part.strip() for part in parts if part.strip()])


def normalize_name(value: str) -> str:
    text = value or ""
    text = re.sub(r"__feishu_[A-Za-z0-9]+", "", text)
    text = re.sub(r"^\d+[_-]", "", text)
    text = re.sub(r"（新版|（待数据|（最终版|\(1|（1|调$", "", text)
    text = re.sub(r"(拍摄)?结算(报告|汇报)?$", "结算", text)
    text = re.sub(r"[\s_（）()【】\[\]《》“”\"'\\-—~&+]+", "", text)
    return text.lower()


def match_score(left: str, right: str) -> float:
    left_norm = normalize_name(left)
    right_norm = normalize_name(right)
    if not left_norm or not right_norm:
        return 0.0
    if left_norm == right_norm:
        return 1.0
    if left_norm in right_norm or right_norm in left_norm:
        return min(len(left_norm), len(right_norm)) / max(
            len(left_norm), len(right_norm)
        )
    return difflib.SequenceMatcher(None, left_norm, right_norm).ratio()


def parse_tracker_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        select c.text
        from documents d
        join chunks c on c.doc_key = d.doc_key
        where d.title = ? and d.doc_type = '项目总表'
        order by d.doc_key, c.chunk_index
        """,
        (TRACKER_TITLE,),
    ).fetchall()
    lines: list[str] = []
    for row in rows:
        for line in str(row["text"] or "").splitlines():
            cleaned = line.strip()
            if cleaned:
                lines.append(cleaned)

    row_types = ("项目类", "汇报类", "结算类", "其他")
    statuses = {
        "已过",
        "不做",
        "调整中",
        "待写",
        "待确认",
        "进行中",
        "未开始",
        "待定",
        "已完成",
        "暂停",
    }
    parsed: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        if not re.fullmatch(r"\d+", line) or index + 4 >= len(lines):
            continue
        if not lines[index + 1].startswith(row_types):
            continue
        owner = lines[index + 4]
        if owner == "/" or owner in statuses or re.match(r"\d{4}\.\d", owner):
            continue
        owners = split_people(owner)
        if not owners:
            continue
        parsed.append(
            {
                "row": int(line),
                "plan_type": lines[index + 1],
                "plan_name": lines[index + 2],
                "progress": lines[index + 3],
                "owner_text": owner,
                "owners": owners,
            }
        )

    seen: set[tuple[str, str]] = set()
    unique_rows: list[dict[str, Any]] = []
    for item in parsed:
        key = (item["plan_name"], item["owner_text"])
        if key in seen:
            continue
        seen.add(key)
        unique_rows.append(item)
    return unique_rows


def best_tracker_match(
    doc_name: str, tracker_rows: list[dict[str, Any]]
) -> tuple[float, dict[str, Any] | None]:
    best_score = 0.0
    best_row: dict[str, Any] | None = None
    for row in tracker_rows:
        score = match_score(doc_name, str(row["plan_name"]))
        if score > best_score:
            best_score = score
            best_row = row
    return best_score, best_row


def departments_for_owners(
    owners: list[str], person_departments: dict[str, str]
) -> list[str]:
    return unique_values([person_departments.get(owner, "") for owner in owners])


def query_candidates(
    conn: sqlite3.Connection,
    tracker_rows: list[dict[str, Any]],
    person_departments: dict[str, str],
    min_confidence: float,
    min_score: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows = conn.execute(
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
        where review_status = 'need_review'
          and confidence >= ?
          and owner = ''
        order by confidence desc, title asc
        """,
        (min_confidence,),
    ).fetchall()

    confirmed: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    for row in rows:
        doc_name = str(row["project_name"] or row["title"] or "")
        score, tracker_row = best_tracker_match(doc_name, tracker_rows)
        if tracker_row is None or score < min_score:
            continue
        owners = list(tracker_row["owners"])
        departments = departments_for_owners(owners, person_departments)
        item = {
            "row": row,
            "score": score,
            "tracker_row": tracker_row,
            "owners": owners,
            "departments": departments,
        }
        if len(departments) == len(owners):
            confirmed.append(item)
        else:
            blocked.append(item)
    return confirmed, blocked


def confirmation_payload(
    row: sqlite3.Row,
    tracker_row: dict[str, Any],
    owners: list[str],
    departments: list[str],
    score: float,
    generated_at: str,
) -> dict[str, Any]:
    return {
        "status": RULE_STATUS,
        "rule": RULE_NAME,
        "confirmed_at": generated_at,
        "previous_review_status": row["review_status"],
        "confidence": float(row["confidence"] or 0),
        "match_score": round(score, 4),
        "owner": "、".join(owners),
        "departments": departments,
        "tracker": {
            "title": TRACKER_TITLE,
            "row": tracker_row["row"],
            "plan_name": tracker_row["plan_name"],
            "plan_type": tracker_row["plan_type"],
            "progress": tracker_row["progress"],
            "owner_text": tracker_row["owner_text"],
        },
        "evidence": [
            "project_tracker_row_match",
            f"match_score>={round(score, 4)}",
            "owners_known_by_override_or_local_people_table",
            "departments_resolved_from_people_rules_or_local_people_table",
        ],
    }


def update_participants(
    existing_json: str, owners: list[str], departments: list[str]
) -> str:
    existing = load_json_array(existing_json)
    participants: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in existing:
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
        participants.append({"name": name, "department": department, "role": role})
    for owner, department in zip(owners, departments, strict=False):
        if owner in seen:
            continue
        seen.add(owner)
        participants.append(
            {"name": owner, "department": department, "role": "项目负责人"}
        )
    return dump_json(participants)


def apply_candidate(
    conn: sqlite3.Connection, item: dict[str, Any], generated_at: str
) -> int:
    row = item["row"]
    owners = item["owners"]
    departments = item["departments"]
    tracker_row = item["tracker_row"]
    score = item["score"]
    confirmation = confirmation_payload(
        row, tracker_row, owners, departments, score, generated_at
    )
    metadata = load_json_dict(row["metadata_json"])
    metadata["rule_confirmation"] = confirmation
    metadata["project_tracker_owner"] = confirmation["tracker"]
    owner_text = "、".join(owners)
    participants_json = update_participants(
        row["participants_json"], owners, departments
    )

    result = conn.execute(
        """
        update documents
        set
            owner = ?,
            departments_json = ?,
            participants_json = ?,
            review_status = ?,
            metadata_json = ?
        where doc_key = ? and review_status = 'need_review' and owner = ''
        """,
        (
            owner_text,
            dump_json(departments),
            participants_json,
            RULE_STATUS,
            dump_json(metadata),
            row["doc_key"],
        ),
    )

    queue_rows = conn.execute(
        """
        select review_id, payload_json
        from review_queue
        where doc_key = ? and status = 'open'
        """,
        (row["doc_key"],),
    ).fetchall()
    for queue_row in queue_rows:
        payload = load_json_dict(queue_row["payload_json"])
        payload["rule_confirmation"] = confirmation
        conn.execute(
            """
            update review_queue
            set status = 'resolved', payload_json = ?
            where review_id = ?
            """,
            (dump_json(payload), queue_row["review_id"]),
        )

    conn.execute(
        """
        update projects
        set owner = ?, departments_json = ?, review_status = ?, updated_at = ?
        where project_id = ? and review_status = 'need_review' and owner = ''
        """,
        (
            owner_text,
            dump_json(departments),
            RULE_STATUS,
            generated_at,
            row["project_id"],
        ),
    )
    return int(result.rowcount or 0)


def run(
    db_path: Path,
    overrides_path: Path,
    min_confidence: float,
    min_score: float,
    apply: bool,
) -> dict[str, Any]:
    overrides = load_overrides(overrides_path)
    person_departments = people_department_map(overrides)
    generated_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        db_people_added = add_db_people(conn, person_departments)
        tracker_rows = parse_tracker_rows(conn)
        confirmed, blocked = query_candidates(
            conn, tracker_rows, person_departments, min_confidence, min_score
        )
        updated_documents = 0
        if apply:
            with conn:
                for item in confirmed:
                    updated_documents += apply_candidate(conn, item, generated_at)

        document_status = {
            str(row["review_status"]): int(row["count"])
            for row in conn.execute(
                "select review_status, count(*) as count from documents group by review_status order by review_status"
            )
        }
        queue_status = {
            str(row["status"]): int(row["count"])
            for row in conn.execute(
                "select status, count(*) as count from review_queue group by status order by status"
            )
        }
    finally:
        conn.close()

    def serialise(item: dict[str, Any]) -> dict[str, Any]:
        row = item["row"]
        return {
            "doc_key": row["doc_key"],
            "title": row["title"],
            "project_name": row["project_name"],
            "doc_type": row["doc_type"],
            "owner": "、".join(item["owners"]),
            "departments": item["departments"],
            "confidence": float(row["confidence"] or 0),
            "match_score": round(item["score"], 4),
            "tracker_row": {
                "row": item["tracker_row"]["row"],
                "plan_name": item["tracker_row"]["plan_name"],
                "owner_text": item["tracker_row"]["owner_text"],
            },
        }

    return {
        "applied": apply,
        "rule": RULE_NAME,
        "min_confidence": min_confidence,
        "min_score": min_score,
        "db_people_added": db_people_added,
        "tracker_rows": len(tracker_rows),
        "known_people": sorted(person_departments),
        "confirmed_candidates": [serialise(item) for item in confirmed],
        "blocked_candidates": [serialise(item) for item in blocked],
        "updated_documents": updated_documents,
        "document_status": document_status,
        "review_queue_status": queue_status,
        "generated_at": generated_at,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--overrides", type=Path, default=DEFAULT_OVERRIDES)
    parser.add_argument("--min-confidence", type=float, default=0.88)
    parser.add_argument("--min-score", type=float, default=0.88)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            run(
                args.db, args.overrides, args.min_confidence, args.min_score, args.apply
            ),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
