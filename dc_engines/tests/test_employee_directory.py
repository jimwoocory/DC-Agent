"""employee_directory.store 单元测试。

覆盖：
- get_or_create: 首次返 (Employee, True)、已存在返 (Employee, False)
- touch: interaction_count + 1、last_seen_at 刷新
- update_profile: 字段独立更新、不动其他字段
- list_employees: 按 last_seen_at DESC 排序、limit 截断
- memory CRUD: add / list（按 relevance + created_at DESC 排序、min_relevance 过滤）/ delete
"""

from __future__ import annotations

import aiosqlite
from dc_engines.employee_directory.store import EmployeeStore


async def test_get_or_create_first_time(employee_store: EmployeeStore) -> None:
    emp, created = await employee_store.get_or_create(
        "ou_abc", platform_id="lark", display_name="张三"
    )
    assert created is True
    assert emp.open_id == "ou_abc"
    assert emp.platform_id == "lark"
    assert emp.display_name == "张三"
    assert emp.relation_type == "employee"
    assert emp.preferred_address == ""
    assert emp.honorific_policy == "formal"
    assert emp.interaction_count == 0
    assert emp.is_anonymous is False


async def test_get_or_create_idempotent(employee_store: EmployeeStore) -> None:
    """第二次 get_or_create 同一 open_id 返 (Employee, False)，不覆盖。"""
    await employee_store.get_or_create("ou_x", platform_id="lark", display_name="x")
    emp2, created2 = await employee_store.get_or_create(
        "ou_x", platform_id="lark", display_name="不应该覆盖"
    )
    assert created2 is False
    assert emp2.display_name == "x"  # 保留原值


async def test_is_anonymous(employee_store: EmployeeStore) -> None:
    emp, _ = await employee_store.get_or_create("ou_anon")
    assert emp.is_anonymous is True
    assert emp.display_name == ""


async def test_touch_increments_interaction_count(
    employee_store: EmployeeStore,
) -> None:
    await employee_store.get_or_create("ou_y", platform_id="lark", display_name="李四")
    for _ in range(3):
        await employee_store.touch("ou_y")
    emp = await employee_store.get_employee("ou_y")
    assert emp is not None
    assert emp.interaction_count == 3


async def test_update_profile_partial(employee_store: EmployeeStore) -> None:
    """update_profile 字段独立更新，不动 None 的字段。"""
    await employee_store.get_or_create("ou_z", platform_id="lark", display_name="王五")
    await employee_store.update_profile("ou_z", department="业务部")
    emp = await employee_store.get_employee("ou_z")
    assert emp is not None
    assert emp.display_name == "王五"  # 没动
    assert emp.department == "业务部"
    assert emp.role == ""  # 还是默认

    # 后续单独改 role 不动 department
    await employee_store.update_profile("ou_z", role="客户经理")
    emp = await employee_store.get_employee("ou_z")
    assert emp.department == "业务部"
    assert emp.role == "客户经理"


async def test_update_profile_identity_fields(employee_store: EmployeeStore) -> None:
    await employee_store.get_or_create(
        "ou_boss", platform_id="lark", display_name="杨国民"
    )
    await employee_store.update_profile(
        "ou_boss",
        relation_type="boss",
        preferred_address="杨总",
        honorific_policy="boss_formal",
        personality_summary="结论导向，重视业务结果",
        communication_style="先结论后依据，避免铺垫过长",
        persona_evidence_count=3,
        persona_updated_at="2026-05-20T00:00:00+00:00",
    )

    emp = await employee_store.get_employee("ou_boss")
    assert emp is not None
    assert emp.relation_type == "boss"
    assert emp.preferred_address == "杨总"
    assert emp.honorific_policy == "boss_formal"
    assert emp.personality_summary == "结论导向，重视业务结果"
    assert emp.communication_style == "先结论后依据，避免铺垫过长"
    assert emp.persona_evidence_count == 3
    assert emp.persona_updated_at == "2026-05-20T00:00:00+00:00"


async def test_initialize_migrates_legacy_employee_schema(tmp_path) -> None:
    db = tmp_path / "legacy_employees.db"
    async with aiosqlite.connect(db) as conn:
        await conn.execute(
            """
            CREATE TABLE employees (
                open_id TEXT PRIMARY KEY,
                platform_id TEXT DEFAULT '',
                display_name TEXT DEFAULT '',
                department TEXT DEFAULT '',
                role TEXT DEFAULT '',
                skill_tags TEXT DEFAULT '[]',
                preferences TEXT DEFAULT '{}',
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                interaction_count INTEGER DEFAULT 0
            )
            """
        )
        await conn.execute(
            """
            INSERT INTO employees (
                open_id, platform_id, display_name, first_seen_at, last_seen_at
            ) VALUES ('ou_old', 'lark', '老大', '2026-01-01', '2026-01-01')
            """
        )
        await conn.commit()

    store = EmployeeStore(db)
    await store.initialize()
    emp = await store.get_employee("ou_old")

    assert emp is not None
    assert emp.display_name == "老大"
    assert emp.relation_type == "employee"
    assert emp.honorific_policy == "formal"
    assert emp.persona_updated_at == ""


async def test_list_employees_limit(employee_store: EmployeeStore) -> None:
    for i in range(5):
        await employee_store.get_or_create(
            f"ou_{i}", platform_id="lark", display_name=f"员工{i}"
        )
    emps = await employee_store.list_employees(limit=3)
    assert len(emps) == 3


async def test_memory_add_and_list(employee_store: EmployeeStore) -> None:
    await employee_store.get_or_create("ou_m", platform_id="lark", display_name="M")
    await employee_store.add_memory("ou_m", "preference", "喜欢咖啡", relevance=0.8)
    await employee_store.add_memory("ou_m", "skill", "python 高手", relevance=0.9)
    await employee_store.add_memory("ou_m", "context", "今天加班", relevance=0.3)

    mems = await employee_store.list_memories("ou_m", limit=10)
    assert len(mems) == 3
    # 按 relevance DESC 排序
    assert mems[0].kind == "skill"
    assert mems[1].kind == "preference"
    assert mems[2].kind == "context"


async def test_memory_min_relevance_filter(employee_store: EmployeeStore) -> None:
    await employee_store.get_or_create("ou_f")
    await employee_store.add_memory("ou_f", "preference", "高分", relevance=0.9)
    await employee_store.add_memory("ou_f", "context", "低分", relevance=0.1)
    mems = await employee_store.list_memories("ou_f", limit=10, min_relevance=0.5)
    assert len(mems) == 1
    assert mems[0].content == "高分"


async def test_memory_delete(employee_store: EmployeeStore) -> None:
    await employee_store.get_or_create("ou_d")
    m = await employee_store.add_memory(
        "ou_d", "preference", "to delete", relevance=0.5
    )
    ok = await employee_store.delete_memory(m.memory_id)
    assert ok is True
    mems = await employee_store.list_memories("ou_d", limit=10)
    assert len(mems) == 0


async def test_memory_delete_nonexistent(employee_store: EmployeeStore) -> None:
    ok = await employee_store.delete_memory("not_a_real_id")
    assert ok is False


async def test_context_injection_trace_add_and_list(
    employee_store: EmployeeStore,
) -> None:
    await employee_store.get_or_create("ou_trace", platform_id="巅池-Agent小助手")
    first_id = await employee_store.add_context_injection(
        "ou_trace",
        platform_id="巅池-Agent小助手",
        relation_type="employee",
        preferred_address="张三",
        honorific_policy="warm",
        memory_ids=["m1"],
        memory_kinds=["fact"],
        included_persona=False,
        block_chars=123,
    )
    second_id = await employee_store.add_context_injection(
        "ou_trace",
        platform_id="巅池-Agent小助手",
        relation_type="employee",
        preferred_address="张三",
        honorific_policy="warm",
        memory_ids=["m2", "m3"],
        memory_kinds=["persona", "fact"],
        included_persona=True,
        block_chars=456,
    )

    traces = await employee_store.list_context_injections("ou_trace", limit=10)

    assert len(traces) == 2
    assert traces[0]["injection_id"] == second_id
    assert traces[0]["memory_ids"] == ["m2", "m3"]
    assert traces[0]["memory_kinds"] == ["persona", "fact"]
    assert traces[0]["included_persona"] is True
    assert traces[0]["block_chars"] == 456
    assert traces[1]["injection_id"] == first_id
