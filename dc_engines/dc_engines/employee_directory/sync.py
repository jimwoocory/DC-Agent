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

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from lark_oapi.api.contact.v3 import ListDepartmentRequest, ListUserRequest

from .store import EmployeeStore

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SyncReport:
    success: bool
    departments_scanned: int = 0
    users_added: int = 0
    users_updated: int = 0
    users_skipped: int = 0
    error: str | None = None
    samples: list[str] = field(default_factory=list)  # 前几条 display_name 用于显示


async def _list_departments(client, root_department_id: str | None = None) -> list[str]:
    """列出所有可见部门 ID（递归）。"""
    dept_ids: list[str] = []
    page_token: str | None = None

    while True:
        try:
            builder = (
                ListDepartmentRequest.builder()
                .department_id_type("open_department_id")
                .user_id_type("open_id")
                .fetch_child(True)
                .page_size(50)
            )
            if root_department_id:
                builder = builder.parent_department_id(root_department_id)
            if page_token:
                builder = builder.page_token(page_token)
            req = builder.build()
            resp = await client._client.contact.v3.department.alist(req)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[employee_sync] list_department 异常: %s", exc)
            return dept_ids

        if not resp.success():
            logger.warning(
                "[employee_sync] list_department code=%s msg=%s",
                getattr(resp, "code", "?"),
                getattr(resp, "msg", "?"),
            )
            return dept_ids

        items = (resp.data and resp.data.items) or []
        for d in items:
            did = getattr(d, "open_department_id", None) or getattr(
                d, "department_id", None
            )
            if did:
                dept_ids.append(did)

        if not (resp.data and resp.data.has_more):
            break
        page_token = resp.data.page_token
        if not page_token:
            break

    return dept_ids


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
) -> SyncReport:
    """同步主入口。

    ``department_name_map``: 可选 open_department_id → 显示名 映射，缺则不填部门名。
    """
    if not client or not client.enabled:
        return SyncReport(success=False, error="Feishu credentials 未启用")

    try:
        dept_ids = await _list_departments(
            client, root_department_id=root_department_id
        )
    except Exception as exc:  # noqa: BLE001
        return SyncReport(success=False, error=f"list_department 失败: {exc}")

    if not dept_ids:
        return SyncReport(
            success=False,
            error="未拿到任何部门——通常是 app 权限不足，需勾 contact:department.base:read",
        )

    seen_open_ids: set[str] = set()
    added = 0
    updated = 0
    skipped = 0
    samples: list[str] = []

    for did in dept_ids:
        users = await _list_users_in_department(client, did)
        for u in users:
            oid = u["open_id"]
            if oid in seen_open_ids:
                skipped += 1
                continue
            seen_open_ids.add(oid)

            existing = await store.get_employee(oid)
            if existing is None:
                # 新增
                emp, _ = await store.get_or_create(
                    oid,
                    platform_id=platform_id,
                    display_name=u["name"],
                )
                # 立即补部门 / 岗位
                dept_name = (department_name_map or {}).get(did) or did[-8:]
                await store.update_profile(
                    oid,
                    department=dept_name,
                    role=u.get("job_title", ""),
                )
                added += 1
                if len(samples) < 5 and u["name"]:
                    samples.append(u["name"])
            else:
                # 更新（不覆盖已有非空字段，按需补）
                upd: dict = {}
                if not existing.display_name and u["name"]:
                    upd["display_name"] = u["name"]
                if not existing.department:
                    dept_name = (department_name_map or {}).get(did) or did[-8:]
                    upd["department"] = dept_name
                if not existing.role and u.get("job_title"):
                    upd["role"] = u["job_title"]
                if upd:
                    await store.update_profile(oid, **upd)
                    updated += 1
                else:
                    skipped += 1

    return SyncReport(
        success=True,
        departments_scanned=len(dept_ids),
        users_added=added,
        users_updated=updated,
        users_skipped=skipped,
        samples=samples,
    )
