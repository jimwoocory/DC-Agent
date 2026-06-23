from __future__ import annotations

from types import SimpleNamespace

from dc_engines.org_permissions import (
    APP_RUNTIME,
    COMPANY_BOSS,
    CONTENT_RULE_REVIEW,
    DC_ADMIN,
    DEPARTMENT_MANAGER,
    FEISHU_ADMIN_OBSERVED,
    HR_OPS,
    OFFICE_OPS,
    PermissionAssignmentStore,
    build_principal_context,
    build_principal_context_from_employee,
    can_view_employee_profile,
    canonical_department_path,
    normalize_department_name,
)


def test_canonical_department_tree_and_aliases() -> None:
    assert normalize_department_name("中台") == "中台部门"
    assert normalize_department_name("策划部") == "策略部"
    assert normalize_department_name("中台-策划") == "策略部"
    assert normalize_department_name("公司") == "总经办"
    assert canonical_department_path("客户部") == ("总经办", "中台部门", "客户部")
    assert canonical_department_path("策划部") == ("总经办", "中台部门", "策略部")
    assert canonical_department_path("综合部") == ("总经办", "执行部门", "综合部")
    assert canonical_department_path("柳汽") == ("总经办", "品宣部门", "柳汽")


def test_feishu_admin_fact_does_not_grant_dc_admin() -> None:
    zhou_fang = build_principal_context(
        subject_id="ou_zhoufang",
        admins_id=("ou_caiting",),
        display_name="周芳",
        department="综合部",
        role="部门经理/负责人",
        relation_type="manager",
        preferences={
            "feishu_is_app_admin": True,
            "feishu_admin_source": "manual_confirmed_by_user",
            "business_responsibilities": ["招聘", "办公室内务管理", "综合部管理"],
            "management_scope": "综合部",
            "is_department_manager": True,
        },
    )

    assert zhou_fang.external_facts["feishu_is_app_admin"] is True
    assert zhou_fang.has_permission(FEISHU_ADMIN_OBSERVED, "feishu")
    assert not zhou_fang.has_permission(DC_ADMIN)
    assert zhou_fang.has_permission(DEPARTMENT_MANAGER, "综合部")
    assert zhou_fang.has_permission(HR_OPS)
    assert zhou_fang.has_permission(OFFICE_OPS)


def test_manager_and_boss_scoped_permissions() -> None:
    luo_ming = build_principal_context(
        subject_id="ou_luoming",
        department="中台",
        role="中台负责人",
        relation_type="manager",
        preferences={"managed_departments": ["策略部", "客户部"]},
    )
    yang_zong = build_principal_context(
        subject_id="ou_yang",
        department="总经办",
        role="Boss/总负责人",
        relation_type="boss",
        preferences={
            "is_company_boss": True,
            "managed_departments": ["执行部门", "品宣部门"],
        },
    )

    assert luo_ming.department == "中台部门"
    assert luo_ming.has_permission(DEPARTMENT_MANAGER, "策略部")
    assert luo_ming.has_permission(DEPARTMENT_MANAGER, "策划部")
    assert luo_ming.has_permission(DEPARTMENT_MANAGER, "客户部")
    assert not luo_ming.has_permission(DC_ADMIN)
    assert yang_zong.has_permission(DEPARTMENT_MANAGER, "执行部门")
    assert yang_zong.has_permission(DEPARTMENT_MANAGER, "品宣部门")
    assert yang_zong.has_permission(DEPARTMENT_MANAGER, "客户部")
    assert not yang_zong.has_permission(DC_ADMIN)


def test_admins_id_and_app_principal_separation() -> None:
    cai_ting = build_principal_context(
        subject_id="ou_caiting",
        admins_id=("ou_caiting",),
        display_name="蔡挺",
        department="数字化应用部",
    )
    assistant_app = build_principal_context(
        subject_id="cli_aa8cc8481e7a9bea",
        admins_id=("ou_caiting",),
        display_name="巅池-Agent小助手",
    )

    assert cai_ting.principal_type == "user"
    assert cai_ting.has_permission(DC_ADMIN)
    assert assistant_app.principal_type == "app"
    assert assistant_app.has_permission(APP_RUNTIME)
    assert not assistant_app.has_permission(DC_ADMIN)


def test_initial_dc_permission_matrix_keeps_org_roles_scoped() -> None:
    cai_ting = build_principal_context(
        subject_id="ou_caiting",
        admins_id=("ou_caiting",),
        display_name="蔡挺",
        department="数字化应用部",
    )
    assistant_app = build_principal_context(
        subject_id="cli_aa8cc8481e7a9bea",
        admins_id=("ou_caiting",),
        display_name="巅池-Agent小助手",
    )
    zhou_fang = build_principal_context(
        subject_id="ou_zhoufang",
        admins_id=("ou_caiting",),
        display_name="周芳",
        department="综合部",
        relation_type="manager",
        preferences={
            "managed_departments": ["综合部"],
            "business_responsibilities": ["招聘", "办公室内务管理"],
            "feishu_is_app_admin": True,
            "feishu_admin_source": "manual_confirmed_by_user",
        },
    )
    luo_ming = build_principal_context(
        subject_id="ou_luoming",
        admins_id=("ou_caiting",),
        display_name="罗鸣",
        department="中台",
        relation_type="manager",
        preferences={"managed_departments": ["客户部", "策略部"]},
    )
    yang_zong = build_principal_context(
        subject_id="ou_yang",
        admins_id=("ou_caiting",),
        display_name="杨总",
        department="公司",
        relation_type="boss",
        preferences={"is_company_boss": True},
    )

    assert cai_ting.has_permission(DC_ADMIN)
    assert assistant_app.has_permission(APP_RUNTIME)
    assert not assistant_app.has_permission(DC_ADMIN)
    assert zhou_fang.has_permission(DEPARTMENT_MANAGER, "综合部")
    assert zhou_fang.has_permission(HR_OPS)
    assert zhou_fang.has_permission(OFFICE_OPS)
    assert zhou_fang.has_permission(FEISHU_ADMIN_OBSERVED, "feishu")
    assert not zhou_fang.has_permission(DC_ADMIN)
    assert luo_ming.has_permission(DEPARTMENT_MANAGER, "客户部")
    assert luo_ming.has_permission(DEPARTMENT_MANAGER, "策略部")
    assert not luo_ming.has_permission(DC_ADMIN)
    assert yang_zong.department == "总经办"
    assert yang_zong.has_permission(COMPANY_BOSS)
    assert yang_zong.has_permission(DEPARTMENT_MANAGER, "客户部")
    assert not yang_zong.has_permission(DC_ADMIN)


def test_department_manager_profile_visibility_follows_org_scope() -> None:
    zhou_fang = build_principal_context(
        subject_id="ou_zhoufang",
        department="综合部",
        relation_type="manager",
        preferences={"managed_departments": ["综合部"]},
    )
    execution_manager = build_principal_context(
        subject_id="ou_execution",
        department="执行部门",
        relation_type="manager",
        preferences={"managed_departments": ["执行部门"]},
    )
    cai_ting = build_principal_context(
        subject_id="ou_caiting",
        admins_id=("ou_caiting",),
    )

    assert can_view_employee_profile(list(zhou_fang.permissions), "综合部")
    assert not can_view_employee_profile(list(zhou_fang.permissions), "客户部")
    assert can_view_employee_profile(list(execution_manager.permissions), "综合部")
    assert not can_view_employee_profile(list(execution_manager.permissions), "客户部")
    assert can_view_employee_profile(list(cai_ting.permissions), "客户部")


async def test_permission_store_persists_assignments_and_syncs_admins_id(
    tmp_path,
) -> None:
    store = PermissionAssignmentStore(tmp_path / "permissions.db")
    await store.initialize()
    await store.sync_admins_id(("ou_caiting", "cli_aa8cc8481e7a9bea"))
    assistant_legacy_permissions = await store.list_assignments("cli_aa8cc8481e7a9bea")
    await store.upsert_assignment(
        subject_id="ou_ops",
        permission=CONTENT_RULE_REVIEW,
        scope="*",
        source="manual",
    )
    await store.upsert_assignment(
        subject_id="cli_aa8cc8481e7a9bea",
        permission=DC_ADMIN,
        scope="*",
        source="manual",
    )

    cai_permissions = await store.list_assignments("ou_caiting")
    assistant_permissions = await store.list_assignments("cli_aa8cc8481e7a9bea")
    ops_permissions = await store.list_assignments("ou_ops")

    assert {(item.permission, item.scope, item.source) for item in cai_permissions} == {
        (DC_ADMIN, "*", "admins_id")
    }
    assert assistant_legacy_permissions == []
    assert {
        (item.permission, item.scope, item.source) for item in assistant_permissions
    } == {(DC_ADMIN, "*", "manual")}
    assert {(item.permission, item.scope, item.source) for item in ops_permissions} == {
        (CONTENT_RULE_REVIEW, "*", "manual")
    }

    principal = build_principal_context(
        subject_id="ou_ops",
        permission_assignments=ops_permissions,
    )

    assert principal.has_permission(CONTENT_RULE_REVIEW)
    assert not principal.has_permission(DC_ADMIN)

    assistant_principal = build_principal_context(
        subject_id="cli_aa8cc8481e7a9bea",
        permission_assignments=await store.list_assignments("cli_aa8cc8481e7a9bea"),
    )

    assert assistant_principal.has_permission(APP_RUNTIME)
    assert not assistant_principal.has_permission(DC_ADMIN)


def test_build_principal_context_from_employee() -> None:
    employee = SimpleNamespace(
        open_id="ou_manager",
        display_name="经理",
        department="中台",
        role="负责人",
        relation_type="manager",
        preferences={"managed_departments": ["客户部"]},
    )

    principal = build_principal_context_from_employee(employee, admins_id=())

    assert principal.department == "中台部门"
    assert principal.managed_departments == ("客户部",)
    assert principal.has_permission(DEPARTMENT_MANAGER, "客户部")
