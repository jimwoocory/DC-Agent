"""P3：从飞书通讯录同步员工到 EmployeeStore。

策略：
1. 列出可见部门（递归子部门）
2. 每个部门里列出用户（分页）
3. 用 ``open_id`` 作主键 upsert 进 ``employees`` 表

需要飞书 app 权限：
- ``contact:user.base:read``     用户基本信息
- ``contact:user.id:read``       用户 ID
- ``contact:department.base:read`` 部门信息

权限不足 / 凭证缺失 → 返回 SyncReport(success=False, error=...)。
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from lark_oapi.api.contact.v3 import ListDepartmentRequest, ListUserRequest

from .org_structure import DepartmentOrgIndex, load_department_org_index
from .store import EmployeeStore

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SyncReport:
    success: bool
    preview_only: bool = False
    plan_id: str = ""
    departments_scanned: int = 0
    department_names: list[str] = field(default_factory=list)
    users_added: int = 0
    users_updated: int = 0
    users_skipped: int = 0
    roster_enriched_count: int = 0
    conflict_count: int = 0
    review_required_count: int = 0
    error: str | None = None
    samples: list[str] = field(default_factory=list)  # 前几条 display_name 用于显示


@dataclass(slots=True)
class IdentitySyncAction:
    """One exact open_id-matched employee change in a sync plan.

    Attributes:
        open_id: Stable Feishu identity used for the match.
        create: Whether the employee row is new.
        updates: Blank-field or explicitly authoritative profile updates.
        conflicts: Existing values that differ and will not be overwritten.
        projected_missing_fields: Identity fields still missing after the plan.
    """

    open_id: str
    create: bool
    updates: dict
    conflicts: tuple[str, ...] = ()
    projected_missing_fields: tuple[str, ...] = ()


@dataclass(slots=True)
class DepartmentRecord:
    open_department_id: str
    name: str = ""
    parent_department_id: str = ""


def _department_display_name(department) -> str:
    name = str(getattr(department, "name", "") or "").strip()
    if name:
        return name
    i18n_name = getattr(department, "i18n_name", None)
    for attr in ("zh_cn", "zh_cn_name", "name"):
        value = str(getattr(i18n_name, attr, "") or "").strip()
        if value:
            return value
    return ""


async def _list_department_records(
    client, root_department_id: str | None = None
) -> list[DepartmentRecord]:
    """列出所有可见部门记录（递归）。"""
    records: list[DepartmentRecord] = []
    page_token: str | None = None
    effective_root_department_id = (
        "0" if root_department_id is None else root_department_id
    )

    while True:
        try:
            builder = (
                ListDepartmentRequest.builder()
                .department_id_type("open_department_id")
                .user_id_type("open_id")
                .fetch_child(True)
                .page_size(50)
            )
            if effective_root_department_id:
                builder = builder.parent_department_id(effective_root_department_id)
            if page_token:
                builder = builder.page_token(page_token)
            req = builder.build()
            resp = await client._client.contact.v3.department.alist(req)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[employee_sync] list_department 异常: %s", exc)
            return records

        if not resp.success():
            logger.warning(
                "[employee_sync] list_department code=%s msg=%s",
                getattr(resp, "code", "?"),
                getattr(resp, "msg", "?"),
            )
            return records

        items = (resp.data and resp.data.items) or []
        for d in items:
            did = getattr(d, "open_department_id", None) or getattr(
                d, "department_id", None
            )
            if not did:
                continue
            parent_id = str(
                getattr(d, "parent_department_id", "")
                or getattr(d, "open_parent_department_id", "")
                or ""
            )
            records.append(
                DepartmentRecord(
                    open_department_id=str(did),
                    name=_department_display_name(d),
                    parent_department_id=parent_id,
                )
            )

        if not (resp.data and resp.data.has_more):
            break
        page_token = resp.data.page_token
        if not page_token:
            break

    return records


async def _list_departments(client, root_department_id: str | None = None) -> list[str]:
    """列出所有可见部门 ID（递归）。"""
    return [
        record.open_department_id
        for record in await _list_department_records(
            client, root_department_id=root_department_id
        )
    ]


def _sync_preferences(
    existing_preferences: dict,
    department_id: str,
    *,
    department_name: str = "",
    org_index: DepartmentOrgIndex | None = None,
) -> dict:
    preferences = {
        **(existing_preferences or {}),
        "org_source": "feishu_contact",
        "feishu_open_department_id": department_id,
    }
    if department_name:
        preferences["feishu_department"] = department_name
    org_info = (
        org_index.lookup(department_name) if org_index and department_name else None
    )
    if org_info is not None:
        preferences["business_parent_department"] = org_info.parent
        preferences["department_path"] = list(org_info.path)
        preferences["department_aliases"] = list(org_info.aliases)
    return preferences


async def _list_users_in_department(client, department_id: str) -> list[dict]:
    """列单个部门内所有用户。返回 list of dicts with open_id/name/department/role."""
    users: list[dict] = []
    page_token: str | None = None

    while True:
        try:
            builder = (
                ListUserRequest.builder()
                .department_id(department_id)
                .department_id_type("open_department_id")
                .user_id_type("open_id")
                .page_size(50)
            )
            if page_token:
                builder = builder.page_token(page_token)
            req = builder.build()
            resp = await client._client.contact.v3.user.alist(req)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[employee_sync] list_user dept=%s 异常: %s", department_id, exc
            )
            return users

        if not resp.success():
            logger.warning(
                "[employee_sync] list_user dept=%s code=%s",
                department_id,
                getattr(resp, "code", "?"),
            )
            return users

        items = (resp.data and resp.data.items) or []
        for u in items:
            open_id = getattr(u, "open_id", None) or getattr(u, "user_id", None)
            if not open_id:
                continue
            users.append(
                {
                    "open_id": open_id,
                    "name": getattr(u, "name", "") or "",
                    "job_title": getattr(u, "job_title", "") or "",
                    "department_id": department_id,
                }
            )

        if not (resp.data and resp.data.has_more):
            break
        page_token = resp.data.page_token
        if not page_token:
            break

    return users


async def sync_from_feishu(
    store: EmployeeStore,
    client,  # FeishuClient
    *,
    platform_id: str = "lark",
    root_department_id: str | None = None,
    department_name_map: dict[str, str] | None = None,
    organization_people: dict[str, dict] | None = None,
    authoritative: bool = False,
    preview_only: bool = False,
    expected_plan_id: str = "",
) -> SyncReport:
    """Plan or apply an open_id-matched Feishu employee sync.

    Args:
        store: Employee directory store receiving approved changes.
        client: Enabled Feishu client used to read contact data.
        platform_id: Platform identifier assigned to new employees.
        root_department_id: Optional Feishu department subtree root.
        department_name_map: Optional department ID to display-name overrides.
        organization_people: Governed roster profiles used only after an exact
            Feishu-name, local-name, and department match.
        authoritative: Whether confirmed remote values may overwrite local values.
        preview_only: Whether to return the plan without writing employee rows.
        expected_plan_id: Exact preview plan required before a confirmed apply.

    Returns:
        Sync report containing the deterministic plan and projected results.
    """
    if not client or not client.enabled:
        return SyncReport(
            success=False,
            preview_only=preview_only,
            error="Feishu credentials 未启用",
        )

    try:
        dept_records = await _list_department_records(
            client, root_department_id=root_department_id
        )
    except Exception as exc:  # noqa: BLE001
        return SyncReport(
            success=False,
            preview_only=preview_only,
            error=f"list_department 失败: {exc}",
        )

    if not dept_records:
        return SyncReport(
            success=False,
            preview_only=preview_only,
            error="未拿到任何部门——通常是 app 权限不足，需勾 contact:department.base:read",
        )

    dept_ids = sorted(record.open_department_id for record in dept_records)
    auto_department_name_map = {
        record.open_department_id: record.name for record in dept_records if record.name
    }
    if department_name_map:
        auto_department_name_map.update(department_name_map)
    org_index = load_department_org_index()

    seen_open_ids: set[str] = set()
    actions: list[IdentitySyncAction] = []
    duplicate_count = 0
    unchanged_count = 0
    conflict_count = 0
    review_required_count = 0
    roster_enriched_count = 0
    samples: list[str] = []

    for did in dept_ids:
        users = await _list_users_in_department(client, did)
        for u in users:
            oid = str(u.get("open_id") or "").strip()
            if not oid:
                continue
            if oid in seen_open_ids:
                duplicate_count += 1
                continue
            seen_open_ids.add(oid)

            dept_name = auto_department_name_map.get(did) or (
                did[-8:] if authoritative else ""
            )
            remote_name = str(u.get("name") or "").strip()
            remote_role = str(u.get("job_title") or "").strip()
            existing = await store.get_employee(oid)
            role_from_roster = False
            roster_profile = (organization_people or {}).get(remote_name)
            if (
                not remote_role
                and existing is not None
                and not str(existing.role or "").strip()
                and remote_name
                and remote_name == str(existing.display_name or "").strip()
                and isinstance(roster_profile, dict)
                and dept_name
                and dept_name == str(existing.department or "").strip()
                and dept_name == str(roster_profile.get("department") or "").strip()
            ):
                remote_role = str(roster_profile.get("role") or "").strip()
                role_from_roster = bool(remote_role)
            remote_values = {
                "display_name": remote_name,
                "department": dept_name,
                "role": remote_role,
            }
            if existing is None:
                updates = {
                    **remote_values,
                    "platform_id": platform_id,
                    "preferences": _sync_preferences(
                        {}, did, department_name=dept_name, org_index=org_index
                    ),
                }
                projected_missing = tuple(
                    field_name
                    for field_name, value in remote_values.items()
                    if not value
                )
                actions.append(
                    IdentitySyncAction(
                        open_id=oid,
                        create=True,
                        updates=updates,
                        projected_missing_fields=projected_missing,
                    )
                )
            else:
                updates: dict = {}
                conflicts: list[str] = []
                for field_name, remote_value in remote_values.items():
                    if not remote_value:
                        continue
                    local_value = str(getattr(existing, field_name, "") or "").strip()
                    if not local_value or authoritative:
                        if local_value != remote_value:
                            updates[field_name] = remote_value
                    elif local_value != remote_value:
                        conflicts.append(field_name)

                desired_preferences = _sync_preferences(
                    {},
                    did,
                    department_name=dept_name,
                    org_index=org_index,
                )
                if role_from_roster:
                    desired_preferences["identity_role_source"] = (
                        "company_org_structure"
                    )
                    roster_enriched_count += 1
                if authoritative:
                    next_preferences = {
                        **existing.preferences,
                        **desired_preferences,
                    }
                else:
                    next_preferences = dict(existing.preferences)
                    for key, remote_value in desired_preferences.items():
                        local_value = next_preferences.get(key)
                        if local_value in (None, "", [], {}):
                            next_preferences[key] = remote_value
                        elif local_value != remote_value:
                            conflicts.append(f"preferences.{key}")
                if next_preferences != existing.preferences:
                    updates["preferences"] = next_preferences

                projected_values = {
                    "display_name": updates.get("display_name", existing.display_name),
                    "department": updates.get("department", existing.department),
                    "role": updates.get("role", existing.role),
                }
                projected_missing = tuple(
                    field_name
                    for field_name, value in projected_values.items()
                    if not str(value or "").strip()
                )
                if updates:
                    actions.append(
                        IdentitySyncAction(
                            open_id=oid,
                            create=False,
                            updates=updates,
                            conflicts=tuple(sorted(set(conflicts))),
                            projected_missing_fields=projected_missing,
                        )
                    )
                else:
                    unchanged_count += 1
                    conflict_count += len(set(conflicts))
                    if projected_missing:
                        review_required_count += 1

            if actions and actions[-1].open_id == oid:
                action = actions[-1]
                conflict_count += len(action.conflicts)
                if action.projected_missing_fields:
                    review_required_count += 1
                display_name = str(
                    action.updates.get("display_name") or u.get("name") or ""
                ).strip()
                if len(samples) < 5 and display_name:
                    samples.append(display_name)

    plan_payload = {
        "authoritative": authoritative,
        "actions": [
            {
                "open_id": action.open_id,
                "create": action.create,
                "updates": action.updates,
                "conflicts": list(action.conflicts),
                "projected_missing_fields": list(action.projected_missing_fields),
            }
            for action in sorted(actions, key=lambda item: item.open_id)
        ],
    }
    plan_id = (
        "identity_"
        + hashlib.sha256(
            json.dumps(
                plan_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:16]
    )
    added = sum(1 for action in actions if action.create)
    updated = sum(1 for action in actions if not action.create)
    report = SyncReport(
        success=True,
        preview_only=preview_only,
        plan_id=plan_id,
        departments_scanned=len(dept_ids),
        department_names=sorted(set(auto_department_name_map.values())),
        users_added=added,
        users_updated=updated,
        users_skipped=duplicate_count + unchanged_count,
        roster_enriched_count=roster_enriched_count,
        conflict_count=conflict_count,
        review_required_count=review_required_count,
        samples=samples,
    )
    if not preview_only and not expected_plan_id:
        report.success = False
        report.error = "identity sync plan_id required; preview first"
        return report
    if expected_plan_id and expected_plan_id != plan_id:
        report.success = False
        report.error = "identity sync plan changed; preview again"
        return report
    if preview_only:
        return report

    for action in actions:
        updates = dict(action.updates)
        if action.create:
            display_name = str(updates.pop("display_name", "") or "")
            updates.pop("platform_id", None)
            await store.get_or_create(
                action.open_id,
                platform_id=platform_id,
                display_name=display_name,
            )
        if updates:
            await store.update_profile(action.open_id, **updates)

    return report
