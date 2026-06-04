from __future__ import annotations

from pathlib import Path

import aiosqlite
from dc_engines.ai_inbox import InboxItemCreateRequest, InboxStore
from dc_engines.case import CaseStore
from dc_engines.company_cognition import (
    CompanyCognitionHealthCheck,
    HistoricalCaseBackfiller,
)
from dc_engines.employee_directory import EmployeeStore
from dc_engines.harness import HarnessTaskCreateRequest, HarnessTaskStore
from dc_engines.harness.memory_store import HarnessMemoryStore


async def _seed_kb_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(path) as db:
        await db.executescript("""
            CREATE TABLE knowledge_bases (
                kb_id TEXT PRIMARY KEY,
                kb_name TEXT NOT NULL,
                doc_count INTEGER NOT NULL DEFAULT 0,
                chunk_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE kb_documents (
                doc_id TEXT PRIMARY KEY,
                kb_id TEXT NOT NULL,
                chunk_count INTEGER NOT NULL DEFAULT 0,
                media_count INTEGER NOT NULL DEFAULT 0
            );
        """)
        await db.execute(
            "INSERT INTO knowledge_bases (kb_id, kb_name, doc_count, chunk_count) VALUES (?, ?, ?, ?)",
            ("kb_1", "nas_knowledge", 1, 3),
        )
        await db.execute(
            "INSERT INTO knowledge_bases (kb_id, kb_name, doc_count, chunk_count) VALUES (?, ?, ?, ?)",
            ("kb_2", "中台运营", 0, 0),
        )
        await db.execute(
            "INSERT INTO kb_documents (doc_id, kb_id, chunk_count, media_count) VALUES (?, ?, ?, ?)",
            ("doc_1", "kb_1", 3, 0),
        )
        await db.commit()


async def test_company_cognition_report_detects_case_gap(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    employee_store = EmployeeStore(data_dir / "employees.db")
    await employee_store.initialize()
    await employee_store.get_or_create("ou_1", platform_id="lark", display_name="张三")
    await employee_store.update_profile(
        "ou_1",
        department="策划",
        role="策划",
        preferred_address="张三",
    )
    await employee_store.add_memory(
        "ou_1",
        "preference",
        "preference: 结论先行",
        relevance=0.8,
    )
    await employee_store.get_or_create("ou_2", platform_id="lark")

    harness_store = HarnessTaskStore(data_dir / "harness.db")
    await harness_store.initialize()
    task = await harness_store.create_task(
        HarnessTaskCreateRequest(
            title="整理投标案例",
            conversation_id="conv_1",
            platform_id="lark",
            session_id="session_1",
            domain="department_workflow:planning",
            payload={"requester_open_id": "ou_1"},
        )
    )
    await harness_store.update_task_status(
        task.task_id,
        "completed",
        result={"summary": "已整理投标案例"},
    )
    memory_store = HarnessMemoryStore(data_dir / "harness_memory.db")
    await memory_store.initialize()
    await memory_store.create_memory(
        session_id="session_1",
        conversation_id="conv_1",
        task_id=task.task_id,
        domain="department_workflow:planning",
        memory_kind="task_outcome",
        title=task.title,
        summary="已整理投标案例",
        payload={},
    )

    inbox_store = InboxStore(data_dir / "ai_inbox.db")
    await inbox_store.initialize()
    await inbox_store.create_item(
        InboxItemCreateRequest(
            session_id="session_1",
            conversation_id="conv_1",
            platform_id="lark",
            sender_id="ou_1",
            sender_name="张三",
            text="帮我整理投标案例",
            category="request",
            status="acknowledged",
            task_id=task.task_id,
        )
    )
    await _seed_kb_db(data_dir / "knowledge_base" / "kb.db")

    report = await CompanyCognitionHealthCheck(tmp_path).build_report()
    data = report.to_dict()

    assert data["components"]["employees"]["total"] == 2
    assert data["components"]["harness"]["total"] == 1
    assert data["coverage"]["harness_completed_memory"] == 1.0
    assert data["verdict"] == "action_required"
    assert any(risk["area"] == "case" for risk in data["risks"])
    assert "中台运营" in data["components"]["knowledge_base"]["empty_expected"]


async def test_historical_case_backfill_is_idempotent(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    harness_store = HarnessTaskStore(data_dir / "harness.db")
    await harness_store.initialize()
    first = await harness_store.create_task(
        HarnessTaskCreateRequest(
            title="整理红标案例",
            conversation_id="conv_1",
            platform_id="lark",
            session_id="session_1",
            domain="department_workflow:planning",
            payload={
                "requester_open_id": "ou_1",
                "requester_display_name": "张三",
                "department": "策划",
            },
        )
    )
    second = await harness_store.create_task(
        HarnessTaskCreateRequest(
            title="真实性资料校验：补充数据",
            conversation_id="conv_1",
            platform_id="lark",
            session_id="session_1",
            domain="truth_intake",
            payload={},
        )
    )
    await harness_store.update_task_status(
        first.task_id,
        "completed",
        result={"summary": "已完成"},
    )

    backfiller = HistoricalCaseBackfiller(data_dir)
    plan = await backfiller.plan()
    assert plan.dry_run is True
    assert len(plan.planned_groups) == 1
    assert plan.planned_groups[0].task_ids == [first.task_id, second.task_id]

    applied = await backfiller.apply()
    assert applied.created == 1
    assert applied.updated == 0

    case_store = CaseStore(data_dir / "cases.db")
    await case_store.initialize()
    case = await case_store.get_case(applied.planned_groups[0].case_id)
    assert case is not None
    assert case.task_ids == [first.task_id, second.task_id]
    assert case.payload["history_backfill_key"].startswith("harness_session:")
    assert case.payload["requester_open_id"] == "ou_1"

    second_apply = await backfiller.apply()
    assert second_apply.created == 0
    assert second_apply.updated == 0
    assert second_apply.planned_groups == []
