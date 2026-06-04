#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "nas_memory.db"
BACKUP_DIR = ROOT / "data"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def backup_db(db_path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = BACKUP_DIR / f"nas_memory.db.before_agy_candidate_queue_{stamp}.db"
    shutil.copy2(db_path, backup)
    return backup


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def load_report(path: Path) -> dict[str, Any]:
    parsed = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return parsed


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        create table if not exists agy_candidate_rules (
            candidate_id text primary key,
            doc_key text not null,
            title text not null default '',
            source_batch text not null default '',
            source_report text not null default '',
            candidate_kind text not null default 'agy_local_accepted',
            suggested_owner text not null default '',
            suggested_departments_json text not null default '[]',
            local_rule text not null default '',
            local_verdict_json text not null default '{}',
            agy_evidence_json text not null default '[]',
            risk_reason text not null default '',
            status text not null default 'open',
            created_at text not null,
            updated_at text not null
        )
        """
    )
    conn.execute(
        "create index if not exists idx_agy_candidate_rules_doc_key on agy_candidate_rules(doc_key)"
    )
    conn.execute(
        "create index if not exists idx_agy_candidate_rules_status on agy_candidate_rules(status)"
    )


def candidate_id(batch_id: str, doc_key: str, kind: str) -> str:
    return f"{batch_id}:{kind}:{doc_key}"


def queue_item(
    report: dict[str, Any], item: dict[str, Any], kind: str, now: str
) -> dict[str, Any]:
    verdict = item.get("local_verdict") or {}
    owner = str(verdict.get("owner") or item.get("agy_owner") or "").strip()
    departments = verdict.get("departments") or item.get("agy_departments") or []
    if not isinstance(departments, list):
        departments = []
    batch_id = str(report.get("batch_id") or "")
    doc_key = str(item.get("doc_key") or "")
    return {
        "candidate_id": candidate_id(batch_id, doc_key, kind),
        "doc_key": doc_key,
        "title": str(item.get("title") or ""),
        "source_batch": batch_id,
        "source_report": str(report.get("source") or ""),
        "candidate_kind": kind,
        "suggested_owner": owner,
        "suggested_departments_json": dumps(departments),
        "local_rule": str(verdict.get("kind") or ""),
        "local_verdict_json": dumps(verdict),
        "agy_evidence_json": dumps(item.get("agy_evidence") or []),
        "risk_reason": str(item.get("agy_risk_reason") or ""),
        "status": "open",
        "created_at": now,
        "updated_at": now,
    }


def apply_queue(
    report_path: Path, include_local_only: bool, dry_run: bool
) -> dict[str, Any]:
    report = load_report(report_path)
    now = utc_now()
    items: list[tuple[str, dict[str, Any]]] = [
        ("agy_local_accepted", item) for item in report.get("accepted") or []
    ]
    if include_local_only:
        items.extend(("local_only", item) for item in report.get("local_only") or [])

    queued = [queue_item(report, item, kind, now) for kind, item in items]
    backup_path: Path | None = None
    inserted = 0
    updated = 0

    if not dry_run:
        backup_path = backup_db(DB_PATH)
        conn = sqlite3.connect(DB_PATH)
        try:
            ensure_table(conn)
            for row in queued:
                exists = conn.execute(
                    "select 1 from agy_candidate_rules where candidate_id = ?",
                    (row["candidate_id"],),
                ).fetchone()
                conn.execute(
                    """
                    insert into agy_candidate_rules (
                        candidate_id,
                        doc_key,
                        title,
                        source_batch,
                        source_report,
                        candidate_kind,
                        suggested_owner,
                        suggested_departments_json,
                        local_rule,
                        local_verdict_json,
                        agy_evidence_json,
                        risk_reason,
                        status,
                        created_at,
                        updated_at
                    ) values (
                        :candidate_id,
                        :doc_key,
                        :title,
                        :source_batch,
                        :source_report,
                        :candidate_kind,
                        :suggested_owner,
                        :suggested_departments_json,
                        :local_rule,
                        :local_verdict_json,
                        :agy_evidence_json,
                        :risk_reason,
                        :status,
                        :created_at,
                        :updated_at
                    )
                    on conflict(candidate_id) do update set
                        title = excluded.title,
                        source_report = excluded.source_report,
                        suggested_owner = excluded.suggested_owner,
                        suggested_departments_json = excluded.suggested_departments_json,
                        local_rule = excluded.local_rule,
                        local_verdict_json = excluded.local_verdict_json,
                        agy_evidence_json = excluded.agy_evidence_json,
                        risk_reason = excluded.risk_reason,
                        updated_at = excluded.updated_at
                    """,
                    row,
                )
                if exists:
                    updated += 1
                else:
                    inserted += 1
            conn.commit()
        finally:
            conn.close()

    return {
        "dry_run": dry_run,
        "source": str(report_path),
        "batch_id": report.get("batch_id"),
        "queued_count": len(queued),
        "inserted": inserted,
        "updated": updated,
        "include_local_only": include_local_only,
        "backup_path": str(backup_path) if backup_path else "",
        "items": [
            {
                "doc_key": row["doc_key"],
                "title": row["title"],
                "kind": row["candidate_kind"],
                "owner": row["suggested_owner"],
                "departments": json.loads(row["suggested_departments_json"]),
                "local_rule": row["local_rule"],
            }
            for row in queued
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("local_validation_json", type=Path)
    parser.add_argument("--include-local-only", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    result = apply_queue(
        args.local_validation_json,
        include_local_only=args.include_local_only,
        dry_run=not args.apply,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
