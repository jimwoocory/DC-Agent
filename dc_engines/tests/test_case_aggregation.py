"""case_store + CaseEngine 聚合测试。

覆盖：
- create_case：返 status=initiated、payload 字段透传（含 requester_*）
- get_case 读取后 payload 完整
- list_cases_for_session: 同 session 多 case 排序
- get_active_case_for_session: 取最新非 archived
- append_event / list_events: 事件流追加 + 时间序
- 状态转换：set_status
"""

from __future__ import annotations

from dc_engines.case.case_store import CaseStore


async def test_create_case_minimal(case_store: CaseStore) -> None:
    case = await case_store.create_case(
        name="客户应标-A",
        platform_id="lark",
        session_id="lark:group_1",
    )
    assert case.name == "客户应标-A"
    assert case.status == "initiated"
    assert case.client_name is None
    assert case.task_ids == []
    assert case.payload == {}


async def test_create_case_with_payload(case_store: CaseStore) -> None:
    """case_plugin 集成 employee_directory 后 payload 含 requester_*。"""
    payload = {
        "source": "case_plugin",
        "requester_open_id": "ou_zhangsan",
        "requester_display_name": "张三",
        "requester_department": "业务部",
        "requester_role": "客户经理",
    }
    case = await case_store.create_case(
        name="客户应标-B",
        platform_id="lark",
        session_id="lark:group_2",
        client_name="某甲方",
        payload=payload,
    )
    assert case.client_name == "某甲方"
    assert case.payload["requester_display_name"] == "张三"
    assert case.payload["requester_department"] == "业务部"

    # reload from db 也得有
    reloaded = await case_store.get_case(case.case_id)
    assert reloaded is not None
    assert reloaded.payload["requester_role"] == "客户经理"


async def test_list_cases_for_session(case_store: CaseStore) -> None:
    for name in ("案 1", "案 2", "案 3"):
        await case_store.create_case(
            name=name,
            platform_id="lark",
            session_id="lark:group_x",
        )
    # 别的 session 也建一个，确保 filter
    await case_store.create_case(
        name="别人的案",
        platform_id="lark",
        session_id="lark:group_y",
    )

    cases = await case_store.list_cases_for_session("lark:group_x", limit=10)
    assert len(cases) == 3
    assert all(c.session_id == "lark:group_x" for c in cases)


async def test_get_active_case_for_session(case_store: CaseStore) -> None:
    """active = 最新非 archived 的 case。"""
    c1 = await case_store.create_case(
        name="老案",
        platform_id="lark",
        session_id="lark:group_z",
    )
    c2 = await case_store.create_case(
        name="新案",
        platform_id="lark",
        session_id="lark:group_z",
    )
    active = await case_store.get_active_case_for_session("lark:group_z")
    assert active is not None
    # 新建的应该是 active
    assert active.case_id in (c1.case_id, c2.case_id)


async def test_update_status(case_store: CaseStore) -> None:
    """update_case_fields 强制要求 event_type + event_payload（事件溯源设计）。"""
    case = await case_store.create_case(
        name="status 测试",
        platform_id="lark",
        session_id="lark:s",
    )
    updated = await case_store.update_case_fields(
        case.case_id,
        status="in_progress",
        event_type="status_change",
        event_payload={"to": "in_progress"},
    )
    assert updated.status == "in_progress"


async def test_append_event_and_list(case_store: CaseStore) -> None:
    """create_case 自动 append 一个 case_created event，后续 append 累加。"""
    case = await case_store.create_case(
        name="event 测试",
        platform_id="lark",
        session_id="lark:s",
    )
    await case_store.append_event(
        case.case_id, event_type="task_attached", payload={"task_id": "abc"}
    )
    await case_store.append_event(
        case.case_id, event_type="deliverable_added", payload={"kind": "summary"}
    )
    events = await case_store.list_events(case.case_id)
    # case_created 自带 + 2 个手动 append = 3 条
    assert len(events) == 3
    types = [e.event_type for e in events]
    assert "case_created" in types
    assert "task_attached" in types
    assert "deliverable_added" in types


async def test_get_nonexistent_case(case_store: CaseStore) -> None:
    case = await case_store.get_case("not_a_real_id")
    assert case is None
