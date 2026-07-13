import importlib.util
import json
import sqlite3
import sys
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
            """
            INSERT INTO employee_accounts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
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
            """
            INSERT INTO employee_identities VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
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
                    "id_active",
                    "emp_active",
                    "feishu",
                    "ou_active",
                    "ou_active",
                    "on_active",
                    "u_active",
                    "active@example.com",
                    "tenant-company",
                ),
            ],
        )


def test_build_snapshot_is_deterministic_minimal_and_sorted(tmp_path: Path) -> None:
    module = load_module()
    db_path = tmp_path / "employees.db"
    create_employee_db(db_path)

    snapshot = module.build_snapshot(
        db_path,
        generated_at="2026-07-13T10:00:00+00:00",
    )

    assert snapshot["schema_version"] == 1
    assert snapshot["generated_at"] == "2026-07-13T10:00:00+00:00"
    assert [record["employee_id"] for record in snapshot["records"]] == [
        "emp_active",
        "emp_pending",
    ]
    assert snapshot["records"][0] == {
        "employee_id": "emp_active",
        "display_name": "Active",
        "department": "AI应用部",
        "title": "负责人",
        "role": "admin",
        "status": "active",
        "relation_type": "manager",
        "tenant_key": "tenant-company",
        "feishu_open_id": "ou_active",
        "feishu_union_id": "on_active",
        "feishu_user_id": "u_active",
    }
    assert all("email" not in record for record in snapshot["records"])
    assert snapshot["digest"] == module.snapshot_digest(snapshot["records"])
    assert module.sign_snapshot(snapshot, "sync-secret") == module.sign_snapshot(
        snapshot,
        "sync-secret",
    )


def test_build_snapshot_requires_both_authorization_tables(tmp_path: Path) -> None:
    module = load_module()
    db_path = tmp_path / "employees.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE employee_accounts (employee_id TEXT PRIMARY KEY)")

    with pytest.raises(RuntimeError, match="employee_identities"):
        module.build_snapshot(db_path)


def test_push_snapshot_sends_canonical_signed_json(monkeypatch, tmp_path: Path) -> None:
    module = load_module()
    db_path = tmp_path / "employees.db"
    create_employee_db(db_path)
    snapshot = module.build_snapshot(
        db_path,
        generated_at="2026-07-13T10:00:00+00:00",
    )
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"ok":true,"count":2}'

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(module.urllib.request, "urlopen", fake_urlopen)

    result = module.push_snapshot(
        "https://example.test/api/internal/employee-access/sync",
        snapshot,
        "sync-secret",
    )

    request = captured["request"]
    assert result == {"ok": True, "count": 2}
    assert captured["timeout"] == 20
    assert json.loads(request.data) == snapshot
    assert request.headers["X-dianchi-timestamp"] == snapshot["generated_at"]
    assert request.headers["X-dianchi-signature"] == module.sign_snapshot(
        snapshot,
        "sync-secret",
    )
