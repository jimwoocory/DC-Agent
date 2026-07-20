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
            contact=SimpleNamespace(v3=SimpleNamespace(department=FakeDepartmentAPI()))
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

    preview = await sync_mod.sync_from_feishu(
        store,
        SimpleNamespace(enabled=True),
        platform_id="巅池-Agent小助手",
        authoritative=True,
        preview_only=True,
    )
    report = await sync_mod.sync_from_feishu(
        store,
        SimpleNamespace(enabled=True),
        platform_id="巅池-Agent小助手",
        authoritative=True,
        expected_plan_id=preview.plan_id,
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
async def test_non_authoritative_sync_preserves_existing_values_and_reports_conflicts(
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

    preview = await sync_mod.sync_from_feishu(
        store,
        SimpleNamespace(enabled=True),
        authoritative=False,
        preview_only=True,
    )
    report = await sync_mod.sync_from_feishu(
        store,
        SimpleNamespace(enabled=True),
        authoritative=False,
        expected_plan_id=preview.plan_id,
    )

    employee = await store.get_employee("ou_designer")
    assert employee is not None
    assert employee.display_name == "本地昵称"
    assert employee.department == "设计部"
    assert employee.role == "主设"
    assert report.conflict_count >= 3
    assert employee.preferences["org_source"] == "feishu_contact"
    assert employee.preferences["feishu_open_department_id"] == "od_design"


@pytest.mark.asyncio
async def test_feishu_sync_preview_is_read_only_and_apply_requires_matching_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = EmployeeStore(tmp_path / "employees.db")
    await store.initialize()
    await store.get_or_create(
        "ou_writer",
        platform_id="巅池-Agent小助手",
        display_name="本地姓名",
    )
    await store.update_profile("ou_writer", department="运营部")

    async def fake_departments(client, root_department_id=None):
        return [sync_mod.DepartmentRecord(open_department_id="od_ops", name="运营部")]

    async def fake_users(client, department_id):
        return [
            {
                "open_id": "ou_writer",
                "name": "远端姓名",
                "job_title": "内容运营",
                "department_id": department_id,
            }
        ]

    monkeypatch.setattr(sync_mod, "_list_department_records", fake_departments)
    monkeypatch.setattr(sync_mod, "_list_users_in_department", fake_users)

    preview = await sync_mod.sync_from_feishu(
        store,
        SimpleNamespace(enabled=True),
        authoritative=False,
        preview_only=True,
    )
    before_apply = await store.get_employee("ou_writer")

    assert preview.success is True
    assert preview.preview_only is True
    assert preview.plan_id
    assert preview.users_updated == 1
    assert preview.conflict_count == 1
    assert preview.review_required_count == 0
    assert before_apply is not None
    assert before_apply.role == ""

    missing_plan = await sync_mod.sync_from_feishu(
        store,
        SimpleNamespace(enabled=True),
        authoritative=False,
    )
    after_missing_plan = await store.get_employee("ou_writer")
    assert missing_plan.success is False
    assert missing_plan.error == "identity sync plan_id required; preview first"
    assert after_missing_plan is not None
    assert after_missing_plan.role == ""

    rejected = await sync_mod.sync_from_feishu(
        store,
        SimpleNamespace(enabled=True),
        authoritative=False,
        expected_plan_id="stale-plan",
    )
    after_rejection = await store.get_employee("ou_writer")
    assert rejected.success is False
    assert rejected.error == "identity sync plan changed; preview again"
    assert after_rejection is not None
    assert after_rejection.role == ""

    applied = await sync_mod.sync_from_feishu(
        store,
        SimpleNamespace(enabled=True),
        authoritative=False,
        expected_plan_id=preview.plan_id,
    )
    employee = await store.get_employee("ou_writer")

    assert applied.success is True
    assert applied.preview_only is False
    assert applied.plan_id == preview.plan_id
    assert employee is not None
    assert employee.display_name == "本地姓名"
    assert employee.department == "运营部"
    assert employee.role == "内容运营"


@pytest.mark.asyncio
async def test_feishu_sync_uses_roster_role_only_after_three_way_identity_match(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = EmployeeStore(tmp_path / "employees.db")
    await store.initialize()
    await store.get_or_create(
        "ou_planner",
        platform_id="巅池-Agent小助手",
        display_name="同名员工",
    )
    await store.update_profile("ou_planner", department="策略部")
    await store.get_or_create(
        "ou_mismatch",
        platform_id="巅池-Agent小助手",
        display_name="部门不一致员工",
    )
    await store.update_profile("ou_mismatch", department="客户部")

    async def fake_departments(client, root_department_id=None):
        return [
            sync_mod.DepartmentRecord(open_department_id="od_strategy", name="策略部")
        ]

    async def fake_users(client, department_id):
        return [
            {
                "open_id": "ou_planner",
                "name": "同名员工",
                "job_title": "",
                "department_id": department_id,
            },
            {
                "open_id": "ou_mismatch",
                "name": "部门不一致员工",
                "job_title": "",
                "department_id": department_id,
            },
        ]

    monkeypatch.setattr(sync_mod, "_list_department_records", fake_departments)
    monkeypatch.setattr(sync_mod, "_list_users_in_department", fake_users)
    organization_people = {
        "同名员工": {"department": "策略部", "role": "策划专员"},
        "部门不一致员工": {"department": "策略部", "role": "项目经理"},
    }

    preview = await sync_mod.sync_from_feishu(
        store,
        SimpleNamespace(enabled=True),
        organization_people=organization_people,
        preview_only=True,
    )
    before_apply = await store.get_employee("ou_planner")

    assert preview.success is True
    assert preview.users_updated == 2
    assert preview.roster_enriched_count == 1
    assert preview.review_required_count == 1
    assert before_apply is not None
    assert before_apply.role == ""

    applied = await sync_mod.sync_from_feishu(
        store,
        SimpleNamespace(enabled=True),
        organization_people=organization_people,
        expected_plan_id=preview.plan_id,
    )
    employee = await store.get_employee("ou_planner")
    mismatch = await store.get_employee("ou_mismatch")

    assert applied.success is True
    assert employee is not None
    assert employee.role == "策划专员"
    assert employee.preferences["identity_role_source"] == "company_org_structure"
    assert mismatch is not None
    assert mismatch.department == "客户部"
    assert mismatch.role == ""
