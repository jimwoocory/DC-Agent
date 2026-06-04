#!/usr/bin/env python3
"""Clear subject/interviewee mentions that were misread as project owners."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DC_ROOT = Path("/Users/dianchi/DC-Agent")
DEFAULT_DB = DC_ROOT / "data" / "nas_memory.db"
RULE_NAME = "subject_mention_not_project_owner"
FALSE_OWNER_DEPARTMENT = "项目协作方"


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


def graph_evidence(value: str | None) -> list[str]:
    metadata = load_json_dict(value)
    evidence = metadata.get("graph_evidence")
    if not isinstance(evidence, list):
        return []
    return [str(item).strip() for item in evidence if str(item).strip()]


def joined_chunk_text(conn: sqlite3.Connection, doc_key: str) -> str:
    rows = conn.execute(
        """
        select text
        from chunks
        where doc_key = ?
        order by chunk_index
        """,
        (doc_key,),
    ).fetchall()
    return "\n".join(str(row["text"] or "") for row in rows)


def subject_context(owner: str, text: str) -> str:
    subject_index = text.find("拍摄对象")
    owner_index = text.find(owner, subject_index if subject_index >= 0 else 0)
    if subject_index < 0 or owner_index < 0:
        return ""
    if owner_index - subject_index > 260:
        return ""
    start = max(0, subject_index - 40)
    end = min(len(text), owner_index + len(owner) + 80)
    return text[start:end].strip()


def is_subject_owner_misread(
    row: sqlite3.Row, text: str
) -> tuple[bool, list[str], str]:
    owner = str(row["owner"] or "").strip()
    departments = [
        str(item).strip()
        for item in load_json_array(row["departments_json"])
        if str(item).strip()
    ]
    evidence = graph_evidence(row["metadata_json"])
    context = subject_context(owner, text)

    if row["review_status"] != "need_review":
        return False, [], ""
    if str(row["doc_type"] or "").strip() != "排期分工":
        return False, [], ""
    if not owner:
        return False, [], ""
    if departments != [FALSE_OWNER_DEPARTMENT]:
        return False, [], ""
    if f"owner_candidate:first_mentioned:{owner}" not in evidence:
        return False, [], ""
    if not context:
        return False, [], ""
    if f"{owner}博士" not in text and owner not in context:
        return False, [], ""

    return (
        True,
        [
            "doc_type=排期分工",
            f"department={FALSE_OWNER_DEPARTMENT}",
            f"owner_candidate:first_mentioned:{owner}",
            "owner_appears_under_拍摄对象_context",
            "subject_or_interviewee_not_project_owner",
        ],
        context,
    )


def query_candidates(conn: sqlite3.Connection) -> list[dict[str, Any]]:
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
          and coalesce(owner, '') <> ''
          and doc_type = '排期分工'
        order by title asc
        """
    ).fetchall()

    candidates: list[dict[str, Any]] = []
    for row in rows:
        text = joined_chunk_text(conn, row["doc_key"])
        matched, evidence, context = is_subject_owner_misread(row, text)
        if not matched:
            continue
        candidates.append({"row": row, "evidence": evidence, "context": context})
    return candidates


def correction_payload(
    row: sqlite3.Row, evidence: list[str], context: str, generated_at: str
) -> dict[str, Any]:
    return {
        "rule": RULE_NAME,
        "corrected_at": generated_at,
        "previous_owner": row["owner"],
        "previous_departments": load_json_array(row["departments_json"]),
        "previous_confidence": float(row["confidence"] or 0),
        "evidence": evidence,
        "context_excerpt": context,
        "action": "clear_owner_and_departments_keep_need_review",
    }


def apply_candidate(
    conn: sqlite3.Connection, item: dict[str, Any], generated_at: str
) -> dict[str, int]:
    row = item["row"]
    correction = correction_payload(
        row, item["evidence"], item["context"], generated_at
    )
    metadata = load_json_dict(row["metadata_json"])
    metadata["owner_correction"] = correction

    doc_result = conn.execute(
        """
        update documents
        set owner = '', departments_json = '[]', confidence = 0, metadata_json = ?
        where doc_key = ? and review_status = 'need_review'
        """,
        (dump_json(metadata), row["doc_key"]),
    )

    queue_updates = 0
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
        payload["owner"] = ""
        payload["departments"] = []
        payload["owner_correction"] = correction
        conn.execute(
            """
            update review_queue
            set payload_json = ?
            where review_id = ?
            """,
            (dump_json(payload), queue_row["review_id"]),
        )
        queue_updates += 1

    project_updates = 0
    if str(row["project_id"] or "").strip():
        project_result = conn.execute(
            """
            update projects
            set owner = '', departments_json = '[]', confidence = 0, updated_at = ?
            where project_id = ?
              and review_status = 'need_review'
              and owner = ?
              and departments_json = ?
            """,
            (
                generated_at,
                row["project_id"],
                row["owner"],
                row["departments_json"],
            ),
        )
        project_updates = int(project_result.rowcount or 0)

    return {
        "documents": int(doc_result.rowcount or 0),
        "review_queue": queue_updates,
        "projects": project_updates,
    }


def status_counts(conn: sqlite3.Connection) -> dict[str, Any]:
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
    owner_dept_need_review = {
        str(row["bucket"]): int(row["count"])
        for row in conn.execute(
            """
            select
                case
                    when coalesce(owner, '') = '' and coalesce(departments_json, '[]') in ('[]', '') then 'missing_owner_dept'
                    when coalesce(owner, '') = '' then 'missing_owner_has_dept'
                    else 'has_owner_has_dept'
                end as bucket,
                count(*) as count
            from documents
            where review_status = 'need_review'
            group by bucket
            order by bucket
            """
        )
    }
    return {
        "document_status": document_status,
        "review_queue_status": queue_status,
        "owner_dept_need_review": owner_dept_need_review,
    }


def run(db_path: Path, apply: bool) -> dict[str, Any]:
    generated_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        candidates = query_candidates(conn)
        totals = {"documents": 0, "review_queue": 0, "projects": 0}
        if apply:
            with conn:
                for item in candidates:
                    updates = apply_candidate(conn, item, generated_at)
                    for key, value in updates.items():
                        totals[key] += value
        counts = status_counts(conn)
    finally:
        conn.close()

    def serialise(item: dict[str, Any]) -> dict[str, Any]:
        row = item["row"]
        return {
            "doc_key": row["doc_key"],
            "title": row["title"],
            "project_name": row["project_name"],
            "doc_type": row["doc_type"],
            "previous_owner": row["owner"],
            "previous_departments": load_json_array(row["departments_json"]),
            "confidence": float(row["confidence"] or 0),
            "rel_path": row["rel_path"],
            "evidence": item["evidence"],
            "context_excerpt": item["context"],
        }

    return {
        "applied": apply,
        "rule": RULE_NAME,
        "candidates": [serialise(item) for item in candidates],
        "updated": totals,
        "generated_at": generated_at,
        **counts,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            run(args.db, args.apply), ensure_ascii=False, indent=2, sort_keys=True
        )
    )


if __name__ == "__main__":
    main()
