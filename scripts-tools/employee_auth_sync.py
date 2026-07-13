#!/usr/bin/env python3
"""Export the local employee directory to the Vercel authorization mirror."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sqlite3
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
DEFAULT_DB_PATH = Path(__file__).resolve().parents[1] / "data" / "employees.db"


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def build_snapshot(
    db_path: Path,
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Build a deterministic employee authorization snapshot.

    Args:
        db_path: SQLite employee directory path.
        generated_at: Optional fixed UTC timestamp for deterministic tests.

    Returns:
        Versioned snapshot containing sorted authorization records.

    Raises:
        RuntimeError: If the database or required identity tables are unavailable.
    """
    path = Path(db_path).expanduser().resolve()
    if not path.is_file():
        raise RuntimeError(f"employee database does not exist: {path}")

    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            available_tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            required_tables = {"employee_accounts", "employee_identities"}
            missing_tables = sorted(required_tables - available_tables)
            if missing_tables:
                raise RuntimeError(
                    "required employee tables are unavailable: "
                    + ", ".join(missing_tables)
                )

            rows = conn.execute(
                """
                SELECT
                    account.employee_id,
                    account.display_name,
                    account.department,
                    account.title,
                    account.role,
                    account.status,
                    account.relation_type,
                    CASE
                        WHEN identity.tenant_key != '' THEN identity.tenant_key
                        ELSE account.tenant_key
                    END AS tenant_key,
                    identity.feishu_open_id,
                    identity.feishu_union_id,
                    identity.feishu_user_id
                FROM employee_accounts AS account
                JOIN employee_identities AS identity
                    ON identity.employee_id = account.employee_id
                    AND identity.provider = 'feishu'
                ORDER BY account.employee_id, identity.identity_id
                """
            ).fetchall()
    except sqlite3.Error as exc:
        raise RuntimeError(
            f"failed to read employee authorization data: {exc}"
        ) from exc

    records = [dict(row) for row in rows]
    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at or datetime.now(UTC).isoformat(),
        "records": records,
    }
    snapshot["digest"] = snapshot_digest(records)
    return snapshot


def snapshot_digest(records: list[dict[str, Any]]) -> str:
    """Return the SHA-256 digest of canonical JSON records.

    Args:
        records: Sorted employee authorization records.

    Returns:
        Lowercase hexadecimal SHA-256 digest.
    """
    return hashlib.sha256(_canonical_json(records)).hexdigest()


def sign_snapshot(snapshot: dict[str, Any], secret: str) -> str:
    """Return a hex HMAC-SHA256 signature for canonical snapshot JSON.

    Args:
        snapshot: Complete authorization snapshot.
        secret: Shared employee synchronization secret.

    Returns:
        Lowercase hexadecimal HMAC-SHA256 signature.

    Raises:
        ValueError: If the synchronization secret is empty.
    """
    if not secret:
        raise ValueError("employee synchronization secret is required")
    return hmac.new(
        secret.encode("utf-8"),
        _canonical_json(snapshot),
        hashlib.sha256,
    ).hexdigest()


def push_snapshot(
    endpoint: str,
    snapshot: dict[str, Any],
    secret: str,
) -> dict[str, Any]:
    """Post the signed snapshot to the Vercel internal sync endpoint.

    Args:
        endpoint: HTTPS Vercel synchronization endpoint.
        snapshot: Complete authorization snapshot.
        secret: Shared employee synchronization secret.

    Returns:
        Parsed JSON response from the synchronization endpoint.

    Raises:
        RuntimeError: If the endpoint rejects the request or returns invalid JSON.
    """
    request = urllib.request.Request(
        endpoint,
        data=_canonical_json(snapshot),
        headers={
            "Content-Type": "application/json",
            "X-Dianchi-Timestamp": str(snapshot["generated_at"]),
            "X-Dianchi-Signature": sign_snapshot(snapshot, secret),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"employee snapshot synchronization failed: {exc}") from exc


def main() -> int:
    """Run the employee authorization snapshot CLI.

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(
        description="Export and synchronize the employee authorization snapshot."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument(
        "--endpoint",
        default=os.environ.get("DESKTOP_EMPLOYEE_SYNC_ENDPOINT", ""),
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    snapshot = build_snapshot(args.db)
    summary: dict[str, Any] = {
        "schema_version": snapshot["schema_version"],
        "count": len(snapshot["records"]),
        "digest": snapshot["digest"],
    }

    if not args.dry_run:
        secret = os.environ.get("DESKTOP_EMPLOYEE_SYNC_SECRET", "").strip()
        if not args.endpoint:
            parser.error("--endpoint or DESKTOP_EMPLOYEE_SYNC_ENDPOINT is required")
        if not secret:
            parser.error("DESKTOP_EMPLOYEE_SYNC_SECRET is required")
        summary["server"] = push_snapshot(args.endpoint, snapshot, secret)

    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
