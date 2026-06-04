#!/usr/bin/env python3
"""Apply conservative rule-based confirmations to NAS memory metadata."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DC_ROOT = Path("/Users/dianchi/DC-Agent")
DEFAULT_DB = DC_ROOT / "data" / "nas_memory.db"
DEFAULT_OVERRIDES = DC_ROOT / "data" / "config" / "nas_memory_overrides.json"
RULE_STATUS = "rule_confirmed"
RULE_NAME = "high_confidence_known_owner_department"


def load_json_array(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item).strip() for item in parsed if str(item).strip()]


def load_json_dict(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def dump_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def load_overrides(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    parsed = json.loads(path.read_text(encoding="utf-8"))
    return parsed if isinstance(parsed, dict) else {}


def known_people_and_departments(
    overrides: dict[str, Any],
) -> tuple[set[str], set[str]]:
    known_people: set[str] = set()
    known_departments: set[str] = set()

    departments = overrides.get("departments") or {}
    if isinstance(departments, dict):
        for department_name, item in departments.items():
            if str(department_name).strip():
                known_departments.add(str(department_name).strip())
            if isinstance(item, dict) and str(item.get("lead") or "").strip():
                known_people.add(str(item["lead"]).strip())

    people = overrides.get("people") or {}
    if isinstance(people, dict):
        for person_name, item in people.items():
            if str(person_name).strip():
                known_people.add(str(person_name).strip())
            if isinstance(item, dict) and str(item.get("department") or "").strip():
                known_departments.add(str(item["department"]).strip())

    return known_people, known_departments


def add_db_people(
    conn: sqlite3.Connection, known_people: set[str], known_departments: set[str]
) -> int:
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
        if name not in known_people:
            added += 1
        known_people.add(name)
        known_departments.add(department)
    return added


def candidate_reason(
    row: sqlite3.Row,
    known_people: set[str],
    known_departments: set[str],
    min_confidence: float,
) -> list[str] | None:
    owner = str(row["owner"] or "").strip()
    departments = load_json_array(row["departments_json"])
    confidence = float(row["confidence"] or 0)

    if row["review_status"] != "need_review":
        return None
    if confidence < min_confidence:
        return None
    if not owner or owner not in known_people:
        return None
    if not str(row["project_name"] or "").strip():
        return None
    if "doc_type" in row.keys() and not str(row["doc_type"] or "").strip():
        return None
    if not departments:
        return None
    unknown_departments = [
        department for department in departments if department not in known_departments
    ]
    if unknown_departments:
        return None

    return [
        f"confidence>={min_confidence:.2f}",
        "owner_known_by_override_or_local_people_table",
        "departments_known_by_override_or_local_people_table",
        "project_name_present",
        "doc_type_present" if "doc_type" in row.keys() else "project_record",
    ]


def query_document_candidates(
    conn: sqlite3.Connection,
    known_people: set[str],
    known_departments: set[str],
    min_confidence: float,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        select
            doc_key,
            title,
            rel_path,
            project_name,
            doc_type,
            owner,
            departments_json,
            confidence,
            review_status,
            metadata_json
        from documents
        where review_status = 'need_review'
        order by confidence desc, title asc
        """
    ).fetchall()

    candidates: list[dict[str, Any]] = []
    for row in rows:
        evidence = candidate_reason(
            row, known_people, known_departments, min_confidence
        )
        if evidence is None:
            continue
        candidates.append({"row": row, "evidence": evidence})
    return candidates


def query_project_candidates(
    conn: sqlite3.Connection,
    known_people: set[str],
    known_departments: set[str],
    min_confidence: float,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        select
            project_id,
            project_name,
            owner,
            departments_json,
            confidence,
            review_status
        from projects
        where review_status = 'need_review'
        order by confidence desc, project_name asc
        """
    ).fetchall()

    candidates: list[dict[str, Any]] = []
    for row in rows:
        evidence = candidate_reason(
            row, known_people, known_departments, min_confidence
        )
        if evidence is None:
            continue
        candidates.append({"row": row, "evidence": evidence})
    return candidates


def confirmation_payload(
    kind: str, row: sqlite3.Row, evidence: list[str], generated_at: str
) -> dict[str, Any]:
    return {
        "status": RULE_STATUS,
        "kind": kind,
        "rule": RULE_NAME,
        "confirmed_at": generated_at,
        "previous_review_status": row["review_status"],
        "confidence": float(row["confidence"] or 0),
        "owner": row["owner"],
        "departments": load_json_array(row["departments_json"]),
        "evidence": evidence,
    }


def apply_document_candidate(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    evidence: list[str],
    generated_at: str,
) -> int:
    confirmation = confirmation_payload("document", row, evidence, generated_at)
    metadata = load_json_dict(row["metadata_json"])
    metadata["rule_confirmation"] = confirmation

    result = conn.execute(
        """
        update documents
        set review_status = ?, metadata_json = ?
        where doc_key = ? and review_status = 'need_review'
        """,
        (RULE_STATUS, dump_json(metadata), row["doc_key"]),
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
    return int(result.rowcount or 0)


def apply_project_candidate(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    evidence: list[str],
    generated_at: str,
) -> int:
    result = conn.execute(
        """
        update projects
        set review_status = ?, updated_at = ?
        where project_id = ? and review_status = 'need_review'
        """,
        (RULE_STATUS, generated_at, row["project_id"]),
    )
    return int(result.rowcount or 0)


def run(
    db_path: Path, overrides_path: Path, min_confidence: float, apply: bool
) -> dict[str, Any]:
    overrides = load_overrides(overrides_path)
    known_people, known_departments = known_people_and_departments(overrides)
    generated_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        db_people_added = add_db_people(conn, known_people, known_departments)
        document_candidates = query_document_candidates(
            conn, known_people, known_departments, min_confidence
        )
        project_candidates = query_project_candidates(
            conn, known_people, known_departments, min_confidence
        )

        updated_documents = 0
        updated_projects = 0
        if apply:
            with conn:
                for item in document_candidates:
                    updated_documents += apply_document_candidate(
                        conn, item["row"], item["evidence"], generated_at
                    )
                for item in project_candidates:
                    updated_projects += apply_project_candidate(
                        conn, item["row"], item["evidence"], generated_at
                    )

        review_status = {
            str(row["review_status"]): int(row["count"])
            for row in conn.execute(
                """
                select review_status, count(*) as count
                from documents
                group by review_status
                order by review_status
                """
            )
        }
        queue_status = {
            str(row["status"]): int(row["count"])
            for row in conn.execute(
                """
                select status, count(*) as count
                from review_queue
                group by status
                order by status
                """
            )
        }
    finally:
        conn.close()

    return {
        "applied": apply,
        "rule": RULE_NAME,
        "min_confidence": min_confidence,
        "db_people_added": db_people_added,
        "known_people": sorted(known_people),
        "known_departments": sorted(known_departments),
        "document_candidates": [
            {
                "doc_key": item["row"]["doc_key"],
                "title": item["row"]["title"],
                "project_name": item["row"]["project_name"],
                "doc_type": item["row"]["doc_type"],
                "owner": item["row"]["owner"],
                "departments": load_json_array(item["row"]["departments_json"]),
                "confidence": float(item["row"]["confidence"] or 0),
                "evidence": item["evidence"],
            }
            for item in document_candidates
        ],
        "project_candidates": [
            {
                "project_id": item["row"]["project_id"],
                "project_name": item["row"]["project_name"],
                "owner": item["row"]["owner"],
                "departments": load_json_array(item["row"]["departments_json"]),
                "confidence": float(item["row"]["confidence"] or 0),
                "evidence": item["evidence"],
            }
            for item in project_candidates
        ],
        "updated_documents": updated_documents,
        "updated_projects": updated_projects,
        "document_status": review_status,
        "review_queue_status": queue_status,
        "generated_at": generated_at,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--overrides", type=Path, default=DEFAULT_OVERRIDES)
    parser.add_argument("--min-confidence", type=float, default=0.95)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    print(
        json.dumps(
            run(args.db, args.overrides, args.min_confidence, args.apply),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
