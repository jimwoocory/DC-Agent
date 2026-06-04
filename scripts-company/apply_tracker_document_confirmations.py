#!/usr/bin/env python3
"""Confirm project tracker documents from explicit project overrides."""

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
RULE_NAME = "project_tracker_document_owner_override"


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


def known_people_and_departments(
    overrides: dict[str, Any],
) -> tuple[set[str], set[str]]:
    people: set[str] = set()
    departments: set[str] = set()

    for department_name, item in (overrides.get("departments") or {}).items():
        if str(department_name).strip():
            departments.add(str(department_name).strip())
        if isinstance(item, dict) and str(item.get("lead") or "").strip():
            people.add(str(item["lead"]).strip())

    for person_name, item in (overrides.get("people") or {}).items():
        if str(person_name).strip():
            people.add(str(person_name).strip())
        if isinstance(item, dict) and str(item.get("department") or "").strip():
            departments.add(str(item["department"]).strip())

    return people, departments


def add_db_people(
    conn: sqlite3.Connection, people: set[str], departments: set[str]
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
        if name not in people:
            added += 1
        people.add(name)
        departments.add(department)
    return added


def tracker_project_overrides(overrides: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for project_name, item in (overrides.get("projects") or {}).items():
        if not isinstance(item, dict):
            continue
        if str(item.get("doc_type") or "").strip() != "项目总表":
            continue
        owner = str(item.get("owner") or "").strip()
        departments = [
            str(value).strip()
            for value in item.get("departments") or []
            if str(value).strip()
        ]
        if not owner or not departments:
            continue
        result[str(project_name).strip()] = item
    return result


def participant_names(value: str | None) -> set[str]:
    names: set[str] = set()
    for item in load_json_array(value):
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
        else:
            name = str(item).strip()
        if name:
            names.add(name)
    return names


def query_candidates(
    conn: sqlite3.Connection,
    overrides: dict[str, dict[str, Any]],
    known_people: set[str],
    known_departments: set[str],
) -> list[dict[str, Any]]:
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
          and coalesce(owner, '') = ''
          and doc_type = '项目总表'
        order by confidence desc, title asc
        """
    ).fetchall()

    candidates: list[dict[str, Any]] = []
    for row in rows:
        project_name = str(row["project_name"] or row["title"] or "").strip()
        override = overrides.get(project_name)
        if not override:
            continue
        owner = str(override.get("owner") or "").strip()
        departments = [
            str(value).strip()
            for value in override.get("departments") or []
            if str(value).strip()
        ]
        if owner not in known_people:
            continue
        if any(department not in known_departments for department in departments):
            continue
        names = participant_names(row["participants_json"])
        if owner not in names:
            continue
        candidates.append(
            {
                "row": row,
                "override": override,
                "owner": owner,
                "departments": departments,
                "evidence": [
                    "doc_type=项目总表",
                    "project_override:nas_memory_overrides.json",
                    "participant_contains_owner",
                    "owner_known_by_override_or_local_people_table",
                    "departments_known_by_override_or_local_people_table",
                    "summary_table_owner_not_row_level_owner",
                ],
            }
        )
    return candidates


def confirmation_payload(item: dict[str, Any], generated_at: str) -> dict[str, Any]:
    row = item["row"]
    override = item["override"]
    confidence = max(
        float(row["confidence"] or 0), float(override.get("confidence") or 0)
    )
    return {
        "status": RULE_STATUS,
        "rule": RULE_NAME,
        "confirmed_at": generated_at,
        "previous_review_status": row["review_status"],
        "confidence": confidence,
        "owner": item["owner"],
        "departments": item["departments"],
        "project_override": {
            "project_name": override.get("project_name") or row["project_name"],
            "doc_type": override.get("doc_type") or row["doc_type"],
            "review_status": override.get("review_status") or RULE_STATUS,
            "evidence": override.get("evidence") or [],
        },
        "evidence": item["evidence"],
    }


def apply_candidate(
    conn: sqlite3.Connection, item: dict[str, Any], generated_at: str
) -> int:
    row = item["row"]
    confirmation = confirmation_payload(item, generated_at)
    metadata = load_json_dict(row["metadata_json"])
    metadata["rule_confirmation"] = confirmation
    metadata["project_tracker_document_owner"] = confirmation["project_override"]
    confidence = confirmation["confidence"]

    result = conn.execute(
        """
        update documents
        set
            owner = ?,
            departments_json = ?,
            confidence = ?,
            review_status = ?,
            metadata_json = ?
        where doc_key = ? and review_status = 'need_review' and coalesce(owner, '') = ''
        """,
        (
            item["owner"],
            dump_json(item["departments"]),
            confidence,
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
        set owner = ?, departments_json = ?, confidence = ?, review_status = ?, updated_at = ?
        where project_id = ? and review_status = 'need_review' and coalesce(owner, '') = ''
        """,
        (
            item["owner"],
            dump_json(item["departments"]),
            confidence,
            RULE_STATUS,
            generated_at,
            row["project_id"],
        ),
    )
    return int(result.rowcount or 0)


def run(db_path: Path, overrides_path: Path, apply: bool) -> dict[str, Any]:
    overrides = load_overrides(overrides_path)
    project_overrides = tracker_project_overrides(overrides)
    known_people, known_departments = known_people_and_departments(overrides)
    generated_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        db_people_added = add_db_people(conn, known_people, known_departments)
        candidates = query_candidates(
            conn, project_overrides, known_people, known_departments
        )
        updated_documents = 0
        if apply:
            with conn:
                for item in candidates:
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
        override = item["override"]
        return {
            "doc_key": row["doc_key"],
            "title": row["title"],
            "project_name": row["project_name"],
            "doc_type": row["doc_type"],
            "owner": item["owner"],
            "departments": item["departments"],
            "confidence": max(
                float(row["confidence"] or 0), float(override.get("confidence") or 0)
            ),
            "rel_path": row["rel_path"],
            "evidence": item["evidence"],
        }

    return {
        "applied": apply,
        "rule": RULE_NAME,
        "db_people_added": db_people_added,
        "tracker_project_overrides": sorted(project_overrides),
        "candidates": [serialise(item) for item in candidates],
        "updated_documents": updated_documents,
        "document_status": document_status,
        "review_queue_status": queue_status,
        "generated_at": generated_at,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--overrides", type=Path, default=DEFAULT_OVERRIDES)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            run(args.db, args.overrides, args.apply),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
