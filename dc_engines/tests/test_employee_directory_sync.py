from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from dc_engines.employee_directory import sync as sync_mod
from dc_engines.employee_directory.org_structure import build_department_org_index
from dc_engines.employee_directory.store import EmployeeStore


@pytest.mark.asyncio
async def test_list_department_records_starts_from_feishu_root_by_default() -> None:
    captured = {}

    class FakeDepartmentAPI:
        async def alist(self, req):
            captured["parent_department_id"] = getattr(
                req, "parent_department_id", None
            )

            return SimpleNamespace(
                success=lambda: True,
                data=SimpleNamespace(items=[], has_more=False, page_token=""),
            )

    fake_client = SimpleNamespace(
        _client=SimpleNamespace(
            contact=SimpleNamespace(
                v3=SimpleNamespace(department=FakeDepartmentAPI())
            )
        )
    )

    await sync_mod._list_department_records(fake_client)

    assert captured["parent_department_id"] == "0"


@pytest.mark.asyncio
async def test_feishu_sync_authoritatively_aligns_local_employee_to_org(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = EmployeeStore(tmp_path / "employees.db")
    await store.initialize()
    await store.get_or_create(
        "ou_activity",
        platform_id="巅池-Agent小助手",
        display_name="旧姓名",
    )
    await store.update_profile(
        "ou_activity",
        department="执行运营",
        role="实习生",
        preferences={"note": "keep"},
    )

    async def fake_departments(client, root_department_id=None):
        return [
            sync_mod.DepartmentRecord(
                open_department_id="od_activity",
                name="活动统筹部",
                parent_department_id="od_execute",
            )
        ]

    async def fake_users(client, department_id):
        return [
            {
                "open_id": "ou_activity",
                "name": "肖焕辉",
                "job_title": "活动统筹",
                "department_id": department_id,
            }
        ]

    monkeypatch.setattr(sync_mod, "_list_department_records", fake_departments)
    monkeypatch.setattr(sync_mod, "_list_users_in_department", fake_users)
    monkeypatch.setattr(
        sync_mod,
        "load_department_org_index",
        lambda: build_department_org_index(
            [
                {
                    "name": "总经办",
                    "children": [
                        {
                            "name": "执行部门",
                            "children": [{"name": "活动统筹部"}],
                        }
                    ],
                }
            ]
        ),
    )

    report = await sync_mod.sync_from_feishu(
        store,
        SimpleNamespace(enabled=True),
        platform_id="巅池-Agent小助手",
    )

    employee = await store.get_employee("ou_activity")
    assert report.success is True
    assert report.departments_scanned == 1
    assert report.department_names == ["活动统筹部"]
    assert report.users_updated == 1
    assert employee is not None
    assert employee.display_name == "肖焕辉"
    assert employee.department == "活动统筹部"
    assert employee.role == "活动统筹"
    assert employee.preferences["note"] == "keep"
    assert employee.preferences["org_source"] == "feishu_contact"
    assert employee.preferences["feishu_open_department_id"] == "od_activity"
    assert employee.preferences["feishu_department"] == "活动统筹部"
    assert employee.preferences["business_parent_department"] == "执行部门"
    assert employee.preferences["department_path"] == [
        "总经办",
        "执行部门",
        "活动统筹部",
    ]


@pytest.mark.asyncio
async def test_feishu_sync_can_preserve_local_profile_when_not_authoritative(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = EmployeeStore(tmp_path / "employees.db")
    await store.initialize()
    await store.get_or_create(
        "ou_designer",
        platform_id="巅池-Agent小助手",
        display_name="本地昵称",
    )
    await store.update_profile("ou_designer", department="设计部", role="主设")

    async def fake_departments(client, root_department_id=None):
        return [
            sync_mod.DepartmentRecord(open_department_id="od_design", name="设计组")
        ]

    async def fake_users(client, department_id):
        return [
            {
                "open_id": "ou_designer",
                "name": "黄柳泉",
                "job_title": "设计",
                "department_id": department_id,
            }
        ]

    monkeypatch.setattr(sync_mod, "_list_department_records", fake_departments)
    monkeypatch.setattr(sync_mod, "_list_users_in_department", fake_users)

    await sync_mod.sync_from_feishu(
        store,
        SimpleNamespace(enabled=True),
        authoritative=False,
    )

    employee = await store.get_employee("ou_designer")
    assert employee is not None
    assert employee.display_name == "本地昵称"
    assert employee.department == "设计部"
    assert employee.role == "主设"
    assert employee.preferences["org_source"] == "feishu_contact"
    assert employee.preferences["feishu_open_department_id"] == "od_design"
