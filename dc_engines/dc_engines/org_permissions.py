"""Organization-aware permission primitives for DC-Agent.

This module keeps three identity layers separate:
- organization facts: departments, managers, bosses, and business scope
- external platform facts: Feishu admin/app facts
- DC runtime permissions: what a subject may do inside DC-Agent
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import aiosqlite

PrincipalType = Literal["user", "app"]

DC_ADMIN = "dc_admin"
DEPARTMENT_MANAGER = "department_manager"
COMPANY_BOSS = "company_boss"
CONTENT_RULE_REVIEW = "content_rule_review"
HR_OPS = "hr_ops"
OFFICE_OPS = "office_ops"
FEISHU_ADMIN_OBSERVED = "feishu_admin_observed"
APP_RUNTIME = "app_runtime"
EMPLOYEE_SELF_SERVICE = "employee_self_service"

ORG_ROOT = "广西巅池文化传媒有限公司"
ORG_EXECUTIVE_OFFICE = "总经办"
ORG_MIDDLE_PLATFORM = "中台部门"
ORG_EXECUTION = "执行部门"
ORG_BRAND = "品宣部门"

CANONICAL_ORG_TREE: dict[str, dict[str, tuple[str, ...]]] = {
    ORG_EXECUTIVE_OFFICE: {
        ORG_MIDDLE_PLATFORM: ("客户部", "策略部"),
        ORG_EXECUTION: (
            "影视制作部",
            "综合部",
            "财务部",
            "设计部",
            "数字化应用部",
            "AI应用部",
            "活动统筹部",
        ),
        ORG_BRAND: ("运营部", "柳汽"),
    }
}

DEPARTMENT_ALIASES = {
    "中台": ORG_MIDDLE_PLATFORM,
    "中台部门": ORG_MIDDLE_PLATFORM,
    "中台-策划": "策略部",
    "中台策划": "策略部",
    "策划": "策略部",
    "策划部": "策略部",
    "策略": "策略部",
    "策略部": "策略部",
    "执行": ORG_EXECUTION,
    "执行部门": ORG_EXECUTION,
    "活动统筹": "活动统筹部",
    "活动统筹部": "活动统筹部",
    "影视": "影视制作部",
    "影视制作": "影视制作部",
    "影视制作部": "影视制作部",
    "设计": "设计部",
    "设计部": "设计部",
    "AI应用": "AI应用部",
    "AI应用部": "AI应用部",
    "ai应用部": "AI应用部",
    "品宣": ORG_BRAND,
    "品宣部门": ORG_BRAND,
    "品宣部": ORG_BRAND,
    "品宣运营": "运营部",
    "运营部": "运营部",
    "柳汽": "柳汽",
    "公司": ORG_EXECUTIVE_OFFICE,
}

APP_PRINCIPAL_PREFIXES = ("cli_", "app_", "bot_")


@dataclass(frozen=True, slots=True)
class PermissionAssignment:
    subject_id: str
    permission: str
    scope: str = "*"
    source: str = "derived"
    subject_type: PrincipalType = "user"
    enabled: bool = True
    updated_at: str = ""

    def to_record(self) -> dict[str, Any]:
        return {
            "subject_id": self.subject_id,
            "subject_type": self.subject_type,
            "permission": self.permission,
            "scope": self.scope,
            "source": self.source,
            "enabled": self.enabled,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class PrincipalContext:
    subject_id: str
    principal_type: PrincipalType
    display_name: str = ""
    department: str = ""
    role: str = ""
    relation_type: str = "employee"
    managed_departments: tuple[str, ...] = ()
    organization_path: tuple[str, ...] = ()
    external_facts: dict[str, Any] = field(default_factory=dict)
    permissions: tuple[PermissionAssignment, ...] = ()

    def has_permission(self, permission: str, scope: str = "*") -> bool:
        return has_permission(self.permissions, permission, scope=scope)


@dataclass(frozen=True, slots=True)
class AuthorizationDecision:
    """Deterministic result of one scoped DC permission check."""

    allowed: bool
    subject_id: str
    permission: str
    scope: str
    reason: str
    matched_assignment: PermissionAssignment | None = None


_PERMISSION_SCHEMA = """
CREATE TABLE IF NOT EXISTS dc_permission_assignments (
    subject_id TEXT NOT NULL,
    subject_type TEXT NOT NULL,
    permission TEXT NOT NULL,
    scope TEXT NOT NULL,
    source TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (subject_id, subject_type, permission, scope, source)
);

CREATE INDEX IF NOT EXISTS idx_dc_permission_assignments_subject
ON dc_permission_assignments(subject_id, enabled);

CREATE INDEX IF NOT EXISTS idx_dc_permission_assignments_permission
ON dc_permission_assignments(permission, scope, enabled);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_assignment(row: Any) -> PermissionAssignment:
    return PermissionAssignment(
        subject_id=row["subject_id"],
        permission=row["permission"],
        scope=row["scope"] or "*",
        source=row["source"] or "manual",
        subject_type="app" if row["subject_type"] == "app" else "user",
        enabled=bool(row["enabled"]),
        updated_at=row["updated_at"] or "",
    )


class PermissionAssignmentStore:
    """SQLite-backed DC runtime permission assignment store."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    async def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(_PERMISSION_SCHEMA)
            await db.commit()

    async def sync_admins_id(
        self,
        admins_id: list[str] | tuple[str, ...],
        *,
        updated_at: str | None = None,
    ) -> None:
        """Mirror legacy admins_id as dc_admin:* without granting app principals."""
        await self.initialize()
        now = updated_at or _now()
        subject_ids = {
            str(item).strip()
            for item in admins_id
            if str(item).strip() and not is_app_principal(str(item).strip())
        }
        async with aiosqlite.connect(self.db_path) as db:
            if subject_ids:
                placeholders = ",".join("?" for _ in subject_ids)
                await db.execute(
                    f"""
                    UPDATE dc_permission_assignments
                    SET enabled = 0, updated_at = ?
                    WHERE source = 'admins_id'
                      AND permission = ?
                      AND subject_id NOT IN ({placeholders})
                    """,
                    [now, DC_ADMIN, *sorted(subject_ids)],
                )
            else:
                await db.execute(
                    """
                    UPDATE dc_permission_assignments
                    SET enabled = 0, updated_at = ?
                    WHERE source = 'admins_id' AND permission = ?
                    """,
                    (now, DC_ADMIN),
                )
            for subject_id in sorted(subject_ids):
                await db.execute(
                    """
                    INSERT INTO dc_permission_assignments (
                        subject_id, subject_type, permission, scope, source,
                        enabled, updated_at
                    )
                    VALUES (?, 'user', ?, '*', 'admins_id', 1, ?)
                    ON CONFLICT(subject_id, subject_type, permission, scope, source)
                    DO UPDATE SET enabled = 1, updated_at = excluded.updated_at
                    """,
                    (subject_id, DC_ADMIN, now),
                )
            await db.commit()

    async def upsert_assignment(
        self,
        *,
        subject_id: str,
        permission: str,
        scope: str = "*",
        source: str = "manual",
        subject_type: PrincipalType | None = None,
        enabled: bool = True,
        updated_at: str | None = None,
    ) -> PermissionAssignment:
        await self.initialize()
        normalized_subject_id = subject_id.strip()
        normalized_scope = normalize_department_name(scope or "*")
        inferred_subject_type: PrincipalType = (
            subject_type
            if subject_type is not None
            else ("app" if is_app_principal(normalized_subject_id) else "user")
        )
        assignment = PermissionAssignment(
            subject_id=normalized_subject_id,
            subject_type=inferred_subject_type,
            permission=permission.strip(),
            scope=normalized_scope,
            source=source.strip() or "manual",
            enabled=enabled,
            updated_at=updated_at or _now(),
        )
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO dc_permission_assignments (
                    subject_id, subject_type, permission, scope, source,
                    enabled, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(subject_id, subject_type, permission, scope, source)
                DO UPDATE SET
                    enabled = excluded.enabled,
                    updated_at = excluded.updated_at
                """,
                (
                    assignment.subject_id,
                    assignment.subject_type,
                    assignment.permission,
                    assignment.scope,
                    assignment.source,
                    1 if assignment.enabled else 0,
                    assignment.updated_at,
                ),
            )
            await db.commit()
        return assignment

    async def list_assignments(
        self,
        subject_id: str,
        *,
        include_disabled: bool = False,
    ) -> list[PermissionAssignment]:
        await self.initialize()
        query = "SELECT * FROM dc_permission_assignments WHERE subject_id = ?"
        params: list[Any] = [subject_id]
        if not include_disabled:
            query += " AND enabled = 1"
        query += " ORDER BY permission, scope, source"
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(query, params)
            rows = await cur.fetchall()
            return [_row_to_assignment(row) for row in rows]

    async def disable_assignment(
        self,
        *,
        subject_id: str,
        permission: str,
        scope: str = "*",
        source: str = "manual",
        subject_type: PrincipalType | None = None,
    ) -> bool:
        await self.initialize()
        inferred_subject_type: PrincipalType = (
            subject_type
            if subject_type is not None
            else ("app" if is_app_principal(subject_id) else "user")
        )
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """
                UPDATE dc_permission_assignments
                SET enabled = 0, updated_at = ?
                WHERE subject_id = ?
                  AND subject_type = ?
                  AND permission = ?
                  AND scope = ?
                  AND source = ?
                """,
                (
                    _now(),
                    subject_id,
                    inferred_subject_type,
                    permission,
                    normalize_department_name(scope or "*"),
                    source,
                ),
            )
            await db.commit()
            return cur.rowcount > 0


def normalize_department_name(name: str) -> str:
    stripped = (name or "").strip()
    return DEPARTMENT_ALIASES.get(stripped, stripped)


def canonical_department_path(department: str) -> tuple[str, ...]:
    normalized = normalize_department_name(department)
    if not normalized:
        return ()
    if normalized == ORG_EXECUTIVE_OFFICE:
        return (ORG_EXECUTIVE_OFFICE,)
    for parent, children in CANONICAL_ORG_TREE[ORG_EXECUTIVE_OFFICE].items():
        if normalized == parent:
            return (ORG_EXECUTIVE_OFFICE, parent)
        if normalized in children:
            return (ORG_EXECUTIVE_OFFICE, parent, normalized)
    return (normalized,)


def is_app_principal(subject_id: str) -> bool:
    return (subject_id or "").startswith(APP_PRINCIPAL_PREFIXES)


def has_permission(
    assignments: tuple[PermissionAssignment, ...] | list[PermissionAssignment],
    permission: str,
    *,
    scope: str = "*",
) -> bool:
    """Check an exact DC permission and organization scope.

    Args:
        assignments: Subject-bound permission assignments to inspect.
        permission: Exact permission required by the caller.
        scope: Exact scope required; ``*`` requires a global assignment.

    Returns:
        Whether one enabled assignment grants the requested authority.
    """

    requested_scope = normalize_department_name(scope)
    for assignment in assignments:
        if not assignment.enabled:
            continue
        if assignment.permission != permission:
            continue
        assignment_scope = normalize_department_name(assignment.scope)
        if assignment_scope == "*" or assignment_scope == requested_scope:
            return True
    return False


def authorize_permission_records(
    *,
    subject_id: str,
    records: Any,
    permission: str,
    scope: str = "*",
) -> AuthorizationDecision:
    """Authorize subject-bound serialized permission records.

    Args:
        subject_id: Runtime Principal subject requesting authorization.
        records: Serialized permission records published by Principal resolution.
        permission: Exact DC runtime permission required by the caller.
        scope: Exact organization scope required by the caller.

    Returns:
        A deterministic decision including the matched assignment when allowed.
    """

    normalized_subject_id = (subject_id or "").strip()
    normalized_permission = (permission or "").strip()
    normalized_scope = normalize_department_name(scope or "*")
    if not normalized_subject_id:
        return AuthorizationDecision(
            allowed=False,
            subject_id="",
            permission=normalized_permission,
            scope=normalized_scope,
            reason="missing_subject",
        )
    if not normalized_permission:
        return AuthorizationDecision(
            allowed=False,
            subject_id=normalized_subject_id,
            permission="",
            scope=normalized_scope,
            reason="missing_permission",
        )
    if not isinstance(records, list | tuple):
        return AuthorizationDecision(
            allowed=False,
            subject_id=normalized_subject_id,
            permission=normalized_permission,
            scope=normalized_scope,
            reason="invalid_permission_records",
        )

    principal_type: PrincipalType = (
        "app" if is_app_principal(normalized_subject_id) else "user"
    )
    for record in records:
        if not isinstance(record, dict):
            continue
        if str(record.get("subject_id") or "").strip() != normalized_subject_id:
            continue
        raw_subject_type = str(record.get("subject_type") or "").strip()
        if raw_subject_type not in {"user", "app"}:
            continue
        record_subject_type: PrincipalType = (
            "app" if raw_subject_type == "app" else "user"
        )
        if record_subject_type != principal_type:
            continue
        enabled = record.get("enabled")
        if enabled is not True and enabled != 1:
            continue
        record_permission = str(record.get("permission") or "").strip()
        record_scope = str(record.get("scope") or "").strip()
        record_source = str(record.get("source") or "").strip()
        if not record_permission or not record_scope or not record_source:
            continue
        if principal_type == "app" and record_permission != APP_RUNTIME:
            continue
        assignment = PermissionAssignment(
            subject_id=normalized_subject_id,
            subject_type=record_subject_type,
            permission=record_permission,
            scope=normalize_department_name(record_scope),
            source=record_source,
            enabled=True,
            updated_at=str(record.get("updated_at") or ""),
        )
        if has_permission([assignment], normalized_permission, scope=normalized_scope):
            return AuthorizationDecision(
                allowed=True,
                subject_id=normalized_subject_id,
                permission=normalized_permission,
                scope=normalized_scope,
                reason="permission_granted",
                matched_assignment=assignment,
            )
    return AuthorizationDecision(
        allowed=False,
        subject_id=normalized_subject_id,
        permission=normalized_permission,
        scope=normalized_scope,
        reason="permission_denied",
    )


def can_view_employee_profile(
    assignments: tuple[PermissionAssignment, ...] | list[PermissionAssignment],
    employee_department: str,
) -> bool:
    """Return whether scoped DC permissions may view an employee profile."""
    if has_permission(assignments, DC_ADMIN):
        return True
    employee_scopes = canonical_department_path(employee_department)
    if not employee_scopes:
        employee_scopes = (normalize_department_name(employee_department),)
    for scope in employee_scopes:
        if has_permission(assignments, DEPARTMENT_MANAGER, scope=scope):
            return True
    return False


def build_principal_context(
    *,
    subject_id: str,
    admins_id: list[str] | tuple[str, ...] = (),
    display_name: str = "",
    department: str = "",
    role: str = "",
    relation_type: str = "employee",
    preferences: dict[str, Any] | None = None,
    permission_assignments: list[PermissionAssignment]
    | tuple[PermissionAssignment, ...]
    | None = None,
) -> PrincipalContext:
    preferences = preferences or {}
    principal_type: PrincipalType = "app" if is_app_principal(subject_id) else "user"
    normalized_department = normalize_department_name(department)
    managed_departments = _managed_departments(
        preferences=preferences,
        department=normalized_department,
        relation_type=relation_type,
    )
    external_facts = {
        "feishu_is_app_admin": bool(preferences.get("feishu_is_app_admin", False)),
        "feishu_admin_source": str(preferences.get("feishu_admin_source") or ""),
    }
    assignments = _derive_permissions(
        subject_id=subject_id,
        principal_type=principal_type,
        admins_id=admins_id,
        relation_type=relation_type,
        preferences=preferences,
        managed_departments=managed_departments,
    )
    assignments = _merge_assignments(
        subject_id,
        principal_type,
        assignments,
        permission_assignments or (),
    )
    return PrincipalContext(
        subject_id=subject_id,
        principal_type=principal_type,
        display_name=display_name,
        department=normalized_department,
        role=role,
        relation_type=relation_type or "employee",
        managed_departments=tuple(managed_departments),
        organization_path=canonical_department_path(normalized_department),
        external_facts=external_facts,
        permissions=tuple(assignments),
    )


def build_principal_context_from_employee(
    employee: Any,
    *,
    admins_id: list[str] | tuple[str, ...] = (),
    permission_assignments: list[PermissionAssignment]
    | tuple[PermissionAssignment, ...]
    | None = None,
) -> PrincipalContext:
    return build_principal_context(
        subject_id=str(getattr(employee, "open_id", "") or ""),
        admins_id=admins_id,
        display_name=str(getattr(employee, "display_name", "") or ""),
        department=str(getattr(employee, "department", "") or ""),
        role=str(getattr(employee, "role", "") or ""),
        relation_type=str(getattr(employee, "relation_type", "") or "employee"),
        preferences=getattr(employee, "preferences", None) or {},
        permission_assignments=permission_assignments,
    )


def _managed_departments(
    *,
    preferences: dict[str, Any],
    department: str,
    relation_type: str,
) -> list[str]:
    raw = preferences.get("managed_departments")
    if isinstance(raw, list):
        departments = [
            normalize_department_name(str(item)) for item in raw if str(item).strip()
        ]
    else:
        departments = []
    if not departments and preferences.get("management_scope") and department:
        departments = [department]
    if not departments and relation_type == "manager" and department:
        departments = [department]
    return _dedupe(departments)


def _derive_permissions(
    *,
    subject_id: str,
    principal_type: PrincipalType,
    admins_id: list[str] | tuple[str, ...],
    relation_type: str,
    preferences: dict[str, Any],
    managed_departments: list[str],
) -> list[PermissionAssignment]:
    assignments: list[PermissionAssignment] = []
    if principal_type == "app":
        assignments.append(
            _assignment(subject_id, principal_type, APP_RUNTIME, "*", "principal_type")
        )
        return assignments

    assignments.append(
        _assignment(
            subject_id, principal_type, EMPLOYEE_SELF_SERVICE, "self", "default"
        )
    )
    if subject_id in {str(item) for item in admins_id}:
        assignments.append(
            _assignment(subject_id, principal_type, DC_ADMIN, "*", "admins_id")
        )
    if preferences.get("feishu_is_app_admin"):
        assignments.append(
            _assignment(
                subject_id,
                principal_type,
                FEISHU_ADMIN_OBSERVED,
                "feishu",
                str(preferences.get("feishu_admin_source") or "employee_profile"),
            )
        )
    if relation_type in {"manager", "boss"} or preferences.get("is_department_manager"):
        for department in managed_departments:
            assignments.append(
                _assignment(
                    subject_id,
                    principal_type,
                    DEPARTMENT_MANAGER,
                    department,
                    "organization_identity",
                )
            )
    if preferences.get("is_company_boss"):
        assignments.append(
            _assignment(
                subject_id, principal_type, DEPARTMENT_MANAGER, "*", "company_boss"
            )
        )
        assignments.append(
            _assignment(
                subject_id, principal_type, COMPANY_BOSS, "*", "organization_identity"
            )
        )
    responsibilities = (
        {
            normalize_department_name(str(item))
            for item in preferences.get("business_responsibilities", [])
        }
        if isinstance(preferences.get("business_responsibilities"), list)
        else set()
    )
    if "招聘" in responsibilities:
        assignments.append(
            _assignment(subject_id, principal_type, HR_OPS, "*", "responsibility")
        )
    if "办公室内务管理" in responsibilities:
        assignments.append(
            _assignment(subject_id, principal_type, OFFICE_OPS, "*", "responsibility")
        )
    return assignments


def _assignment(
    subject_id: str,
    subject_type: PrincipalType,
    permission: str,
    scope: str,
    source: str,
) -> PermissionAssignment:
    return PermissionAssignment(
        subject_id=subject_id,
        subject_type=subject_type,
        permission=permission,
        scope=normalize_department_name(scope),
        source=source,
        enabled=True,
        updated_at=_now(),
    )


def _merge_assignments(
    subject_id: str,
    principal_type: PrincipalType,
    derived: list[PermissionAssignment],
    persisted: list[PermissionAssignment] | tuple[PermissionAssignment, ...],
) -> list[PermissionAssignment]:
    merged: dict[tuple[str, str, str, str, str], PermissionAssignment] = {}
    for assignment in [*derived, *persisted]:
        if not assignment.enabled:
            continue
        if assignment.subject_id != subject_id:
            continue
        if assignment.subject_type != principal_type:
            continue
        if principal_type == "app" and assignment.permission != APP_RUNTIME:
            continue
        key = (
            assignment.subject_id,
            assignment.subject_type,
            assignment.permission,
            normalize_department_name(assignment.scope),
            assignment.source,
        )
        merged[key] = PermissionAssignment(
            subject_id=assignment.subject_id,
            subject_type=assignment.subject_type,
            permission=assignment.permission,
            scope=normalize_department_name(assignment.scope),
            source=assignment.source,
            enabled=True,
            updated_at=assignment.updated_at,
        )
    return list(merged.values())


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out
