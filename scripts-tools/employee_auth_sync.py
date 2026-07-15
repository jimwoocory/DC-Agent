#!/usr/bin/env python3
"""Export employee identity and company permissions to the Vercel mirror."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import hmac
import json
import os
import sqlite3
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = ROOT / "data" / "employees.db"
DEFAULT_PERMISSIONS_DB_PATH = ROOT / "data" / "permissions.db"
DEFAULT_LOCK_PATH = Path("/tmp/dc-agent-employee-auth-sync.lock")
SYNC_REASONS = {
    "scheduled_reconcile",
    "identity_changed",
    "permission_changed",
    "manual_recovery",
}
PERMISSION_FIELDS = (
    "subject_id",
    "subject_type",
    "permission",
    "scope",
    "source",
    "enabled",
    "updated_at",
)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _principal_key(record: dict[str, Any]) -> str:
    tenant_key = str(record.get("tenant_key") or "").strip()
    for kind, field in (
        ("union", "feishu_union_id"),
        ("user", "feishu_user_id"),
        ("open", "feishu_open_id"),
        ("employee", "employee_id"),
    ):
        value = str(record.get(field) or "").strip()
        if value:
            return f"feishu:{tenant_key or 'unknown'}:{kind}:{value}"
    raise RuntimeError("employee authorization record has no stable identity")


def _load_permissions(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise RuntimeError(f"permission database does not exist: {path}")
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'dc_permission_assignments'"
            ).fetchone()
            if table is None:
                raise RuntimeError(
                    "required permission table is unavailable: "
                    "dc_permission_assignments"
                )
            rows = conn.execute(
                """
                SELECT subject_id, subject_type, permission, scope, source,
                       enabled, updated_at
                FROM dc_permission_assignments
                ORDER BY subject_id, permission, scope, source
                """
            ).fetchall()
    except sqlite3.Error as exc:
        raise RuntimeError(f"failed to read company permissions: {exc}") from exc
    return [
        {
            "subject_id": str(row["subject_id"] or "").strip(),
            "subject_type": str(row["subject_type"] or "").strip(),
            "permission": str(row["permission"] or "").strip(),
            "scope": str(row["scope"] or "*").strip() or "*",
            "source": str(row["source"] or "").strip(),
            "enabled": bool(row["enabled"]),
            "updated_at": str(row["updated_at"] or "").strip(),
        }
        for row in rows
    ]


def build_snapshot(
    db_path: Path,
    permissions_db_path: Path = DEFAULT_PERMISSIONS_DB_PATH,
    *,
    generated_at: str | None = None,
    sync_reason: str = "manual_recovery",
) -> dict[str, Any]:
    """Build a deterministic identity and authorization snapshot.

    Args:
        db_path: SQLite employee directory path.
        permissions_db_path: SQLite company permission database path.
        generated_at: Optional fixed UTC timestamp for deterministic tests.
        sync_reason: Structured reason for this projection run.

    Returns:
        Versioned snapshot containing sorted employee and permission records.

    Raises:
        RuntimeError: If a required database, table, or stable identity is absent.
        ValueError: If the sync reason is unsupported.
    """
    if sync_reason not in SYNC_REASONS:
        raise ValueError(f"unsupported sync reason: {sync_reason}")
    path = Path(db_path).expanduser().resolve()
    permission_path = Path(permissions_db_path).expanduser().resolve()
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
            accounts = conn.execute(
                """
                SELECT employee_id, display_name, department, title, role,
                       status, relation_type, tenant_key
                FROM employee_accounts
                ORDER BY employee_id
                """
            ).fetchall()
            identities = conn.execute(
                """
                SELECT employee_id, provider, provider_subject,
                       feishu_open_id, feishu_union_id, feishu_user_id,
                       tenant_key
                FROM employee_identities
                WHERE provider = 'feishu' OR provider LIKE 'feishu:%'
                ORDER BY employee_id, provider, provider_subject
                """
            ).fetchall()
    except sqlite3.Error as exc:
        raise RuntimeError(
            f"failed to read employee authorization data: {exc}"
        ) from exc

    identities_by_employee: dict[str, list[dict[str, str]]] = {}
    for row in identities:
        provider = str(row["provider"] or "feishu").strip() or "feishu"
        item = {
            "app_key": provider.split(":", 1)[1] if ":" in provider else provider,
            "provider": provider,
            "provider_subject": str(row["provider_subject"] or "").strip(),
            "tenant_key": str(row["tenant_key"] or "").strip(),
            "feishu_open_id": str(row["feishu_open_id"] or "").strip(),
            "feishu_union_id": str(row["feishu_union_id"] or "").strip(),
            "feishu_user_id": str(row["feishu_user_id"] or "").strip(),
        }
        identities_by_employee.setdefault(str(row["employee_id"]), []).append(item)

    permissions = _load_permissions(permission_path)
    permissions_by_subject: dict[str, list[dict[str, Any]]] = {}
    for assignment in permissions:
        if assignment["subject_type"] != "user":
            continue
        permissions_by_subject.setdefault(assignment["subject_id"], []).append(
            assignment
        )

    records: list[dict[str, Any]] = []
    bound_permission_keys: set[tuple[Any, ...]] = set()
    fallback_identity_count = 0
    for account in accounts:
        employee_id = str(account["employee_id"])
        employee_identities = identities_by_employee.get(employee_id, [])
        if not employee_identities:
            raise RuntimeError(
                f"employee authorization record has no Feishu identity: {employee_id}"
            )
        primary = sorted(
            employee_identities,
            key=lambda item: (
                not bool(item["feishu_union_id"]),
                not bool(item["feishu_user_id"]),
                not bool(item["feishu_open_id"]),
                item["provider"],
                item["provider_subject"],
            ),
        )[0]
        tenant_key = primary["tenant_key"] or str(account["tenant_key"] or "")
        record: dict[str, Any] = {
            "employee_id": employee_id,
            "display_name": str(account["display_name"] or ""),
            "department": str(account["department"] or ""),
            "title": str(account["title"] or ""),
            "role": str(account["role"] or "employee"),
            "status": str(account["status"] or "pending"),
            "relation_type": str(account["relation_type"] or "employee"),
            "tenant_key": tenant_key,
            "feishu_open_id": primary["feishu_open_id"],
            "feishu_union_id": primary["feishu_union_id"],
            "feishu_user_id": primary["feishu_user_id"],
            "identities": employee_identities,
        }
        record["principal_key"] = _principal_key(record)
        if ":union:" not in record["principal_key"]:
            fallback_identity_count += 1

        candidates = {employee_id}
        for identity in employee_identities:
            candidates.update(
                value
                for value in (
                    identity["provider_subject"],
                    identity["feishu_open_id"],
                    identity["feishu_union_id"],
                    identity["feishu_user_id"],
                )
                if value
            )
        record_permissions: list[dict[str, Any]] = []
        for subject_id in sorted(candidates):
            for assignment in permissions_by_subject.get(subject_id, []):
                key = tuple(assignment[field] for field in PERMISSION_FIELDS)
                if key in bound_permission_keys:
                    continue
                bound_permission_keys.add(key)
                record_permissions.append(assignment)
        record["permissions"] = sorted(
            record_permissions,
            key=lambda item: tuple(str(item[field]) for field in PERMISSION_FIELDS),
        )
        records.append(record)

    generated = generated_at or datetime.now(UTC).isoformat()
    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "snapshot_id": f"snap_{uuid.uuid4().hex}",
        "generated_at": generated,
        "sync_reason": sync_reason,
        "records": records,
        "stats": {
            "record_count": len(records),
            "identity_count": sum(len(item["identities"]) for item in records),
            "identity_fallback_count": fallback_identity_count,
            "permission_count": sum(len(item["permissions"]) for item in records),
            "unbound_permission_count": len(permissions) - len(bound_permission_keys),
        },
    }
    snapshot["digest"] = snapshot_digest(records)
    return snapshot


def snapshot_digest(records: list[dict[str, Any]]) -> str:
    """Return the SHA-256 digest of canonical employee records.

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
    *,
    attempts: int = 3,
) -> dict[str, Any]:
    """Post the signed snapshot with bounded exponential retry.

    Args:
        endpoint: HTTPS Vercel synchronization endpoint.
        snapshot: Complete authorization snapshot.
        secret: Shared employee synchronization secret.
        attempts: Maximum network attempts.

    Returns:
        Parsed JSON response from the synchronization endpoint.

    Raises:
        RuntimeError: If every attempt fails or the server returns invalid JSON.
    """
    if not endpoint.lower().startswith("https://"):
        raise ValueError("employee synchronization endpoint must use HTTPS")
    last_error: Exception | None = None
    for attempt in range(max(1, attempts)):
        request = urllib.request.Request(
            endpoint,
            data=_canonical_json(snapshot),
            headers={
                "Content-Type": "application/json",
                "X-Dianchi-Timestamp": str(snapshot["generated_at"]),
                "X-Dianchi-Signature": sign_snapshot(snapshot, secret),
                "X-Dianchi-Snapshot-Id": str(snapshot["snapshot_id"]),
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt + 1 < max(1, attempts):
                time.sleep(min(4, 2**attempt))
    raise RuntimeError(
        f"employee snapshot synchronization failed: {last_error}"
    ) from last_error


@contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    """Prevent overlapping scheduled and database-triggered sync runs.

    Args:
        path: Local lock file path.

    Yields:
        Control while the exclusive process lock is held.

    Raises:
        RuntimeError: If another synchronization process owns the lock.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                "employee authorization sync is already running"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


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
        "--permissions-db",
        type=Path,
        default=DEFAULT_PERMISSIONS_DB_PATH,
    )
    parser.add_argument("--lock-file", type=Path, default=DEFAULT_LOCK_PATH)
    parser.add_argument(
        "--reason",
        choices=sorted(SYNC_REASONS),
        default="scheduled_reconcile",
    )
    parser.add_argument(
        "--endpoint",
        default=os.environ.get("DESKTOP_EMPLOYEE_SYNC_ENDPOINT", ""),
    )
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        with exclusive_lock(args.lock_file):
            snapshot = build_snapshot(
                args.db,
                args.permissions_db,
                sync_reason=args.reason,
            )
            summary: dict[str, Any] = {
                "ok": True,
                "schema_version": snapshot["schema_version"],
                "snapshot_id": snapshot["snapshot_id"],
                "digest": snapshot["digest"],
                "sync_reason": snapshot["sync_reason"],
                **snapshot["stats"],
            }

            if not args.dry_run:
                secret = os.environ.get("DESKTOP_EMPLOYEE_SYNC_SECRET", "").strip()
                if not args.endpoint:
                    raise RuntimeError(
                        "--endpoint or DESKTOP_EMPLOYEE_SYNC_ENDPOINT is required"
                    )
                if not secret:
                    raise RuntimeError("DESKTOP_EMPLOYEE_SYNC_SECRET is required")
                summary["server"] = push_snapshot(
                    args.endpoint,
                    snapshot,
                    secret,
                    attempts=args.attempts,
                )
    except (RuntimeError, ValueError) as exc:
        print(
            json.dumps(
                {"ok": False, "error": type(exc).__name__, "detail": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1

    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
