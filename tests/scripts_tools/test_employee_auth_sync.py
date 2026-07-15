import importlib.util
import json
import sqlite3
import sys
import urllib.error
from pathlib import Path

import pytest

MODULE_PATH = (
    Path(__file__).resolve().parents[2] / "scripts-tools" / "employee_auth_sync.py"
)


def load_module():
    spec = importlib.util.spec_from_file_location("employee_auth_sync", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def create_employee_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE employee_accounts (
                employee_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                department TEXT NOT NULL,
                title TEXT NOT NULL,
                role TEXT NOT NULL,
                status TEXT NOT NULL,
                relation_type TEXT NOT NULL,
                email TEXT NOT NULL,
                tenant_key TEXT NOT NULL
            );
            CREATE TABLE employee_identities (
                identity_id TEXT PRIMARY KEY,
                employee_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                provider_subject TEXT NOT NULL,
                feishu_open_id TEXT NOT NULL,
                feishu_union_id TEXT NOT NULL,
                feishu_user_id TEXT NOT NULL,
                email TEXT NOT NULL,
                tenant_key TEXT NOT NULL
            );
            """
        )
        conn.executemany(
            "INSERT INTO employee_accounts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "emp_pending",
                    "Pending",
                    "AI应用部",
                    "助理",
                    "employee",
                    "pending",
                    "employee",
                    "pending@example.com",
                    "tenant-company",
                ),
                (
                    "emp_active",
                    "Active",
                    "AI应用部",
                    "负责人",
                    "admin",
                    "active",
                    "manager",
                    "active@example.com",
                    "tenant-company",
                ),
            ],
        )
        conn.executemany(
            "INSERT INTO employee_identities VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "id_pending",
                    "emp_pending",
                    "feishu",
                    "ou_pending",
                    "ou_pending",
                    "on_pending",
                    "u_pending",
                    "pending@example.com",
                    "tenant-company",
                ),
                (
                    "id_active_agent",
                    "emp_active",
                    "feishu:agent",
                    "ou_active_agent",
                    "ou_active_agent",
                    "on_active",
                    "u_active",
                    "active@example.com",
                    "tenant-company",
                ),
                (
                    "id_active_promo",
                    "emp_active",
                    "feishu:promo",
                    "ou_active_promo",
                    "ou_active_promo",
                    "on_active",
                    "u_active",
                    "active@example.com",
                    "tenant-company",
                ),
            ],
        )


def create_permission_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE dc_permission_assignments (
                subject_id TEXT NOT NULL,
                subject_type TEXT NOT NULL,
                permission TEXT NOT NULL,
                scope TEXT NOT NULL,
                source TEXT NOT NULL,
                enabled INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (
                    subject_id, subject_type, permission, scope, source
                )
            );
            """
        )
        conn.executemany(
            "INSERT INTO dc_permission_assignments VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "ou_active_agent",
                    "user",
                    "dc_admin",
                    "*",
                    "manual",
                    1,
                    "2026-07-15T00:00:00+00:00",
                ),
                (
                    "ou_pending",
                    "user",
                    "office_ops",
                    "AI应用部",
                    "manual",
                    0,
                    "2026-07-15T00:00:00+00:00",
                ),
                (
                    "app_assistant",
                    "app",
                    "dc_admin",
                    "*",
                    "manual",
                    1,
                    "2026-07-15T00:00:00+00:00",
                ),
            ],
        )


def test_build_snapshot_projects_identities_permissions_and_primary_key(
    tmp_path: Path,
) -> None:
    module = load_module()
    db_path = tmp_path / "employees.db"
    permissions_path = tmp_path / "permissions.db"
    create_employee_db(db_path)
    create_permission_db(permissions_path)

    snapshot = module.build_snapshot(
        db_path,
        permissions_path,
        generated_at="2026-07-15T10:00:00+00:00",
        sync_reason="permission_changed",
    )

    assert snapshot["schema_version"] == 2
    assert snapshot["generated_at"] == "2026-07-15T10:00:00+00:00"
    assert snapshot["sync_reason"] == "permission_changed"
    assert snapshot["snapshot_id"].startswith("snap_")
    assert [record["employee_id"] for record in snapshot["records"]] == [
        "emp_active",
        "emp_pending",
    ]
    active = snapshot["records"][0]
    assert active["principal_key"] == "feishu:tenant-company:union:on_active"
    assert [identity["app_key"] for identity in active["identities"]] == [
        "agent",
        "promo",
    ]
    assert active["permissions"] == [
        {
            "subject_id": "ou_active_agent",
            "subject_type": "user",
            "permission": "dc_admin",
            "scope": "*",
            "source": "manual",
            "enabled": True,
            "updated_at": "2026-07-15T00:00:00+00:00",
        }
    ]
    pending = snapshot["records"][1]
    assert pending["permissions"][0]["enabled"] is False
    assert snapshot["stats"] == {
        "record_count": 2,
        "identity_count": 3,
        "identity_fallback_count": 0,
        "permission_count": 2,
        "unbound_permission_count": 1,
    }
    assert all("email" not in record for record in snapshot["records"])
    assert snapshot["digest"] == module.snapshot_digest(snapshot["records"])


def test_build_snapshot_requires_identity_and_permission_tables(tmp_path: Path) -> None:
    module = load_module()
    db_path = tmp_path / "employees.db"
    permissions_path = tmp_path / "permissions.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE employee_accounts (employee_id TEXT PRIMARY KEY)")
    with sqlite3.connect(permissions_path) as conn:
        conn.execute("CREATE TABLE placeholder (id TEXT)")

    with pytest.raises(RuntimeError, match="employee_identities"):
        module.build_snapshot(db_path, permissions_path)

    create_employee_db(db_path := tmp_path / "employees-complete.db")
    with pytest.raises(RuntimeError, match="dc_permission_assignments"):
        module.build_snapshot(db_path, permissions_path)


def test_push_snapshot_retries_and_sends_canonical_signed_json(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = load_module()
    db_path = tmp_path / "employees.db"
    permissions_path = tmp_path / "permissions.db"
    create_employee_db(db_path)
    create_permission_db(permissions_path)
    snapshot = module.build_snapshot(
        db_path,
        permissions_path,
        generated_at="2026-07-15T10:00:00+00:00",
    )
    captured = {}
    attempts = 0

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"ok":true,"count":2}'

    def fake_urlopen(request, timeout):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise urllib.error.URLError("temporary")
        captured["request"] = request
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(module.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)

    result = module.push_snapshot(
        "https://example.test/api/internal/employee-access/sync",
        snapshot,
        "sync-secret",
    )

    request = captured["request"]
    assert attempts == 2
    assert result == {"ok": True, "count": 2}
    assert captured["timeout"] == 20
    assert json.loads(request.data) == snapshot
    assert request.headers["X-dianchi-timestamp"] == snapshot["generated_at"]
    assert request.headers["X-dianchi-snapshot-id"] == snapshot["snapshot_id"]
    assert request.headers["X-dianchi-signature"] == module.sign_snapshot(
        snapshot,
        "sync-secret",
    )


def test_exclusive_lock_rejects_overlapping_run(tmp_path: Path) -> None:
    module = load_module()
    lock_path = tmp_path / "sync.lock"

    with module.exclusive_lock(lock_path):
        with pytest.raises(RuntimeError, match="already running"):
            with module.exclusive_lock(lock_path):
                pass
