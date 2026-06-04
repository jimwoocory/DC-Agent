"""harness 引擎 task 生命周期单元测试。

覆盖：
- create_task → pending
- mark_in_progress → in_progress
- complete_task → completed（含 result dict）
- fail_task → failed（含 reason）
- mark_review_required + approve_task → approved
- mark_review_required + reject_task → rejected
- list_tasks_for_conversation 排序 / 过滤
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
from dc_engines.harness.content_sop_runtime import (
    plan_content_sop_dispatch,
    settle_content_sop_result,
)
from dc_engines.harness.contracts import HarnessTaskCreateRequest
from dc_engines.harness.engine import HarnessEngine
from dc_engines.harness.memory_promotion import HarnessMemoryPromoter
from dc_engines.harness.memory_store import HarnessMemoryStore
from dc_engines.harness.task_store import HarnessTaskStore
from dc_engines.harness.workflows import (
    build_workflow_plan,
    create_workflow_request,
    validate_workflow_result,
)


@pytest_asyncio.fixture
async def harness_engine(tmp_path: Path) -> HarnessEngine:
    store = HarnessTaskStore(str(tmp_path / "harness.db"))
    await store.initialize()
    return HarnessEngine(store)


def _make_request(
    title: str = "测试任务",
    conversation_id: str = "conv_1",
    payload: dict | None = None,
) -> HarnessTaskCreateRequest:
    return HarnessTaskCreateRequest(
        title=title,
        conversation_id=conversation_id,
        platform_id="lark",
        session_id="lark:user_1",
        domain="marketing",
        payload=payload or {"workflow_kind": "marketing_plan"},
    )


async def test_create_task_starts_pending(harness_engine: HarnessEngine) -> None:
    task = await harness_engine.create_task(_make_request())
    assert task.status == "pending"
    assert task.title == "测试任务"
    assert task.conversation_id == "conv_1"
    assert task.payload["workflow_kind"] == "marketing_plan"


async def test_mark_in_progress(harness_engine: HarnessEngine) -> None:
    task = await harness_engine.create_task(_make_request())
    updated = await harness_engine.mark_in_progress(task.task_id, note="开始处理")
    assert updated.status == "in_progress"


async def test_complete_task(harness_engine: HarnessEngine) -> None:
    task = await harness_engine.create_task(_make_request())
    await harness_engine.mark_in_progress(task.task_id)
    completed = await harness_engine.complete_task(
        task.task_id,
        result={"summary": "搞定", "output_url": "/tmp/out.md"},
    )
    assert completed.status == "completed"
    assert completed.result.get("summary") == "搞定"


async def test_fail_task(harness_engine: HarnessEngine) -> None:
    task = await harness_engine.create_task(_make_request())
    await harness_engine.mark_in_progress(task.task_id)
    failed = await harness_engine.fail_task(task.task_id, reason="LLM 超时")
    assert failed.status == "failed"


async def test_approve_workflow(harness_engine: HarnessEngine) -> None:
    """approve_task 写 review record，task 自身 status 不变（保留 review_required，
    表示已审过；业务下一步要 complete_task 推到 completed）。
    HarnessTaskStatus 合法值: pending/in_progress/blocked/review_required/completed/cancelled/failed
    """
    task = await harness_engine.create_task(_make_request())
    await harness_engine.mark_in_progress(task.task_id)
    await harness_engine.mark_review_required(task.task_id, reviewer_note="待审核")

    review = await harness_engine.approve_task(
        task.task_id, reviewer_id="ou_boss", note="OK 发布"
    )
    assert review.decision == "approved"
    assert review.reviewer_id == "ou_boss"

    # task status 仍是 review_required（approve 不推进，只盖审章；下一步需要 complete_task）
    reloaded = await harness_engine.store.get_task(task.task_id)
    assert reloaded is not None
    assert reloaded.status == "review_required"


async def test_reject_workflow(harness_engine: HarnessEngine) -> None:
    """reject_task 写 review record + 把 task status 推到 'blocked'（不是 'rejected'，
    rejected 是 ReviewDecision 字段，不是 TaskStatus）。"""
    task = await harness_engine.create_task(_make_request())
    await harness_engine.mark_in_progress(task.task_id)
    await harness_engine.mark_review_required(task.task_id)

    review = await harness_engine.reject_task(
        task.task_id, reviewer_id="ou_boss", note="不够细"
    )
    assert review.decision == "rejected"
    assert review.note == "不够细"

    reloaded = await harness_engine.store.get_task(task.task_id)
    assert reloaded is not None
    assert reloaded.status == "blocked"


async def test_list_tasks_for_conversation_order(harness_engine: HarnessEngine) -> None:
    # 3 个 task 在同一 conversation
    for i in range(3):
        await harness_engine.create_task(
            _make_request(title=f"task #{i}", conversation_id="conv_x")
        )
    # 别的 conversation 也加一个，确保 filter 生效
    await harness_engine.create_task(
        _make_request(title="不该出现", conversation_id="conv_y")
    )

    tasks = await harness_engine.store.list_tasks_for_conversation("conv_x", limit=5)
    assert len(tasks) == 3
    titles = [t.title for t in tasks]
    assert "不该出现" not in titles


async def test_payload_carries_requester_meta(harness_engine: HarnessEngine) -> None:
    """workflow_intent_plugin 集成 employee_directory 后会塞 requester_*；
    验证 payload 字段透传到 task 不丢。"""
    payload = {
        "workflow_kind": "marketing_plan",
        "requester_open_id": "ou_zhangsan",
        "requester_display_name": "张三",
        "requester_department": "业务部",
    }
    task = await harness_engine.create_task(_make_request(payload=payload))
    assert task.payload["requester_display_name"] == "张三"
    assert task.payload["requester_department"] == "业务部"
    assert task.payload["requester_open_id"] == "ou_zhangsan"

    # reload from db 也得有
    reloaded = await harness_engine.store.get_task(task.task_id)
    assert reloaded is not None
    assert reloaded.payload["requester_display_name"] == "张三"


async def test_merge_payload_updates_task_and_records_event(
    harness_engine: HarnessEngine,
) -> None:
    task = await harness_engine.create_task(
        _make_request(payload={"source": "llm_router_truth_intake"})
    )

    updated = await harness_engine.merge_payload(
        task.task_id,
        {"archive_dir": "/tmp/harness_intake/raw/20260603/intake123"},
        event_type="truth_materials_payload_attached",
    )

    assert updated.payload["source"] == "llm_router_truth_intake"
    assert updated.payload["archive_dir"].endswith("intake123")
    events = await harness_engine.store.list_events(task.task_id)
    assert any(
        event.event_type == "truth_materials_payload_attached" for event in events
    )


async def test_merge_payload_rejects_terminal_task(
    harness_engine: HarnessEngine,
) -> None:
    task = await harness_engine.create_task(_make_request())
    await harness_engine.complete_task(task.task_id, result={"summary": "done"})

    with pytest.raises(RuntimeError, match="terminal task"):
        await harness_engine.merge_payload(task.task_id, {"archive_dir": "/tmp/nope"})


async def test_create_task_attaches_harness_guardrails(
    harness_engine: HarnessEngine,
) -> None:
    task = await harness_engine.create_task(
        _make_request(
            title="给老板看前先确认私聊内容",
            payload={
                "workflow_kind": "project_followup",
                "brief": "老板要结论先行，同事执行风险先别发群里",
                "message_text": "私聊里提到预算和客户数据，需要确认来源。",
            },
        )
    )

    guardrails = task.payload["guardrails"]
    assert guardrails["policy_version"] == "2026-05-21"
    assert "boss_formal_address_required" in guardrails["required_rules"]
    assert "colleague_persona_boundary_required" in guardrails["required_rules"]
    assert "private_chat_scope_required" in guardrails["required_rules"]
    assert "business_fact_source_required" in guardrails["required_rules"]
    assert "boss_skill_candidate" in guardrails["routing_hints"]
    assert "colleague_skill_candidate" in guardrails["routing_hints"]

    events = await harness_engine.store.list_events(task.task_id)
    assert any(event.event_type == "guardrails_attached" for event in events)


def test_employee_memory_identity_audit_workflow_is_registered() -> None:
    plan = build_workflow_plan(
        "employee_memory_identity_audit",
        "检查杨总称呼和员工记忆注入",
        source="concierge_feature",
        message_text="检查员工记忆和杨总称呼护栏",
    )

    assert plan.workflow_kind == "employee_memory_identity_audit"
    assert plan.domain == "employee_memory"
    assert plan.title.startswith("员工身份记忆治理审计")
    assert plan.payload["required_outputs"] == [
        "entrypoint_coverage",
        "identity_policy",
        "memory_eval",
        "boss_guard",
        "regression_cases",
        "next_actions",
    ]


def test_employee_memory_identity_audit_workflow_validation() -> None:
    request = create_workflow_request(
        "employee_memory_identity_audit",
        "灰度验收员工记忆护栏",
        conversation_id="conv_identity",
        platform_id="巅池-Agent小助手",
        session_id="session_identity",
        source="test",
        message_text="员工记忆灰度",
    )
    validation = validate_workflow_result(
        request.payload,
        {
            "entrypoint_coverage": "飞书日常 LLM hook 已覆盖",
            "identity_policy": "已定义固定称呼和敬语策略",
            "memory_eval": "身份覆盖、长期记忆、画像覆盖均可评估",
            "boss_guard": "杨总回复出口自检已启用",
            "regression_cases": ["直呼姓名纠偏", "普通员工不误改"],
            "next_actions": ["飞书真机灰度"],
        },
    )

    assert request.domain == "employee_memory"
    assert validation is not None
    assert validation.workflow_kind == "employee_memory_identity_audit"
    assert validation.is_valid is True


def test_employee_memory_identity_audit_validation_requires_all_outputs() -> None:
    request = create_workflow_request(
        "employee_memory_identity_audit",
        "检查缺项",
        conversation_id="conv_identity",
        platform_id="巅池-Agent小助手",
        session_id="session_identity",
        source="test",
        message_text="员工记忆灰度",
    )
    validation = validate_workflow_result(
        request.payload,
        {"identity_policy": "已定义"},
    )

    assert validation is not None
    assert validation.is_valid is False
    assert validation.missing_outputs == [
        "entrypoint_coverage",
        "memory_eval",
        "boss_guard",
        "regression_cases",
        "next_actions",
    ]


def test_content_sop_workflow_validation_requires_media_outputs() -> None:
    request = create_workflow_request(
        "content_sop_workflow",
        "客户部文案、生图和视频脚本",
        conversation_id="conv_content_sop",
        platform_id="巅池-Agent小助手",
        session_id="session_content_sop",
        source="test",
        message_text="帮客户部做文案、生图 prompt 和视频脚本",
    )

    validation = validate_workflow_result(
        request.payload,
        {
            "message_draft": "客户邀约文案",
            "image_prompt": "一张温暖的活动邀约海报",
        },
    )

    assert request.domain == "content_sop"
    assert validation is not None
    assert validation.workflow_kind == "content_sop_workflow"
    assert validation.is_valid is False
    assert validation.missing_outputs == [
        "video_script",
        "source_citations",
        "review_checklist",
    ]


def test_content_sop_workflow_validation_accepts_full_media_outputs() -> None:
    request = create_workflow_request(
        "content_sop_workflow",
        "策划短视频内容包",
        conversation_id="conv_content_sop_ok",
        platform_id="巅池-Agent小助手",
        session_id="session_content_sop_ok",
        source="test",
        message_text="策划短视频脚本、生图 prompt 和审查清单",
    )

    validation = validate_workflow_result(
        request.payload,
        {
            "message_draft": "发布文案",
            "image_prompt": "模型提示词",
            "video_script": "分镜脚本",
            "source_citations": [{"source_path": "projects/demo.md"}],
            "review_checklist": ["核对品牌口径", "核对活动权益"],
        },
    )

    assert validation is not None
    assert validation.is_valid is True


def test_content_sop_workflow_validation_blocks_missing_sources() -> None:
    request = create_workflow_request(
        "content_sop_workflow",
        "客户邀约内容包",
        conversation_id="conv_content_sop_sources",
        platform_id="巅池-Agent小助手",
        session_id="session_content_sop_sources",
        source="test",
        message_text="客户邀约文案、生图 prompt 和视频脚本",
    )
    request.payload.update(
        {
            "truth_requirements": ["不得编造优惠、价格、客户身份或合作承诺。"],
            "review_required_by_default": True,
            "generation_allowed": True,
            "missing_required_inputs": [],
            "source_citations": [],
        }
    )

    validation = validate_workflow_result(
        request.payload,
        {
            "message_draft": "客户邀约文案",
            "image_prompt": "模型提示词",
            "video_script": "视频脚本",
            "review_checklist": ["核对权益"],
        },
    )

    assert validation is not None
    assert validation.is_valid is False
    assert "事实敏感内容缺少来源依据。" in validation.missing_outputs


async def test_content_sop_dispatch_blocks_needs_materials(
    harness_engine: HarnessEngine,
) -> None:
    task = await harness_engine.create_task(
        _make_request(
            title="内容 SOP 缺资料",
            payload={
                "workflow_kind": "content_sop_workflow",
                "brief": "帮我写客户邀约文案并配图",
                "lifecycle_stage": "needs_materials",
                "generation_allowed": False,
                "missing_required_inputs": [{"key": "audience", "label": "客户"}],
            },
        )
    )

    decision = plan_content_sop_dispatch(task)

    assert decision.should_dispatch is False
    assert decision.action == "send_material_intake"
    assert decision.hermes_payload is None


async def test_content_sop_dispatch_ready_builds_hermes_payload(
    harness_engine: HarnessEngine,
) -> None:
    task = await harness_engine.create_task(
        _make_request(
            title="内容 SOP ready",
            payload={
                "workflow_kind": "content_sop_workflow",
                "brief": "客户部内容包",
                "department_id": "client_dept",
                "scenario_id": "client_content_package",
                "content_type": "mixed",
                "lifecycle_stage": "ready_for_generation",
                "generation_allowed": True,
                "knowledge_context": "来源路径: projects/demo.md",
                "source_citations": [{"source_path": "projects/demo.md"}],
                "expected_outputs": [{"key": "message_draft"}],
                "creative_assumptions": [],
            },
        )
    )

    decision = plan_content_sop_dispatch(task)

    assert decision.should_dispatch is True
    assert decision.action == "dispatch_hermes"
    assert decision.hermes_payload is not None
    assert decision.hermes_payload["workflow_kind"] == "content_sop_workflow"
    assert decision.hermes_payload["department_id"] == "client_dept"
    assert decision.hermes_payload["source_citations"][0]["source_path"].endswith(
        "demo.md"
    )


async def test_content_sop_result_settlement_moves_valid_result_to_review(
    harness_engine: HarnessEngine,
) -> None:
    task = await harness_engine.create_task(
        _make_request(
            title="内容 SOP 结果",
            payload={
                "workflow_kind": "content_sop_workflow",
                "brief": "客户内容包",
                "required_outputs": [
                    "message_draft",
                    "image_prompt",
                    "video_script",
                    "source_citations",
                    "review_checklist",
                ],
                "review_required_by_default": True,
                "generation_allowed": True,
                "missing_required_inputs": [],
                "source_citations": [{"source_path": "projects/demo.md"}],
            },
        )
    )
    await harness_engine.mark_in_progress(task.task_id)

    settled = await settle_content_sop_result(
        harness_engine,
        task,
        {
            "message_draft": "客户触达话术",
            "image_prompt": "业务说明: 海报\n模型 Prompt: 海报",
            "video_script": "视频脚本",
            "source_citations": [{"source_path": "projects/demo.md"}],
            "review_checklist": ["核对权益"],
        },
    )

    assert settled.status == "review_required"
    assert settled.result["lifecycle_stage"] == "review_required"


async def test_content_sop_result_settlement_fails_incomplete_result(
    harness_engine: HarnessEngine,
) -> None:
    task = await harness_engine.create_task(
        _make_request(
            title="内容 SOP 缺结果",
            payload={
                "workflow_kind": "content_sop_workflow",
                "brief": "客户内容包",
                "required_outputs": [
                    "message_draft",
                    "image_prompt",
                    "video_script",
                    "source_citations",
                    "review_checklist",
                ],
                "review_required_by_default": True,
                "generation_allowed": True,
                "missing_required_inputs": [],
                "source_citations": [{"source_path": "projects/demo.md"}],
            },
        )
    )
    await harness_engine.mark_in_progress(task.task_id)

    settled = await settle_content_sop_result(
        harness_engine,
        task,
        {"message_draft": "客户触达话术"},
    )

    assert settled.status == "failed"


async def test_append_trace_rejects_terminal_task(
    harness_engine: HarnessEngine,
) -> None:
    task = await harness_engine.create_task(_make_request())
    await harness_engine.complete_task(task.task_id, result={"summary": "已完成"})

    with pytest.raises(RuntimeError, match="terminal task"):
        await harness_engine.append_trace(
            task.task_id,
            "late_trace",
            {"detail": "should fail"},
        )


async def test_latest_task_excludes_terminal_by_default(
    harness_engine: HarnessEngine,
) -> None:
    task = await harness_engine.create_task(_make_request(conversation_id="conv_done"))
    await harness_engine.complete_task(task.task_id, result={"summary": "已完成"})

    active = await harness_engine.store.get_latest_task_for_conversation("conv_done")
    with_terminal = await harness_engine.store.get_latest_task_for_conversation(
        "conv_done",
        include_terminal=True,
    )

    assert active is None
    assert with_terminal is not None
    assert with_terminal.task_id == task.task_id


async def test_create_task_attaches_session_and_cognitive_context(
    tmp_path: Path,
) -> None:
    async def session_snapshot(conversation_id: str) -> dict:
        return {"conversation_id": conversation_id, "topic": "周报复盘"}

    def cognitive_snapshot(request: HarnessTaskCreateRequest) -> dict:
        return {
            "workflow_kind": request.payload["workflow_kind"],
            "risk": "low",
        }

    store = HarnessTaskStore(str(tmp_path / "harness.db"))
    await store.initialize()
    engine = HarnessEngine(
        store,
        session_snapshot_getter=session_snapshot,
        cognitive_snapshot_getter=cognitive_snapshot,
    )

    task = await engine.create_task(_make_request(conversation_id="conv_context"))

    assert task.payload["session_context"]["topic"] == "周报复盘"
    assert task.payload["cognitive_context"]["workflow_kind"] == "marketing_plan"

    events = await engine.store.list_events(task.task_id)
    event_types = {event.event_type for event in events}
    assert "session_context_linked" in event_types
    assert "cognitive_context_linked" in event_types


async def test_complete_task_promotes_summary_memory(tmp_path: Path) -> None:
    task_store = HarnessTaskStore(str(tmp_path / "harness.db"))
    memory_store = HarnessMemoryStore(tmp_path / "harness_memory.db")
    await task_store.initialize()

    engine = HarnessEngine(
        task_store,
        memory_promoter=HarnessMemoryPromoter(memory_store),
    )
    task = await engine.create_task(
        _make_request(title="营销复盘", conversation_id="conv_memory")
    )

    await engine.complete_task(
        task.task_id,
        result={"summary": "用户更关注交付确定性，需要在下轮方案中前置风险说明。"},
    )

    record = await memory_store.get_by_task(task.task_id, "task_outcome")
    assert record is not None
    assert record.session_id == task.session_id
    assert record.conversation_id == "conv_memory"
    assert record.summary.startswith("用户更关注交付确定性")

    events = await engine.store.list_events(task.task_id)
    assert any(event.event_type == "memory_promoted" for event in events)


async def test_memory_promotion_skips_empty_result(tmp_path: Path) -> None:
    task_store = HarnessTaskStore(str(tmp_path / "harness.db"))
    memory_store = HarnessMemoryStore(tmp_path / "harness_memory.db")
    await task_store.initialize()

    engine = HarnessEngine(
        task_store,
        memory_promoter=HarnessMemoryPromoter(memory_store),
    )
    task = await engine.create_task(_make_request(title="空结果任务"))

    await engine.complete_task(task.task_id, result={"notes": ""})

    record = await memory_store.get_by_task(task.task_id, "task_outcome")
    assert record is None

    events = await engine.store.list_events(task.task_id)
    assert not any(event.event_type == "memory_promoted" for event in events)
