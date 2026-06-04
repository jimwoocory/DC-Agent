from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from .contracts import CognitionRisk, CompanyCognitionReport
from .memory_matrix import MemoryMatrixHealthCheck

EXPECTED_KB_NAMES: tuple[str, ...] = (
    "nas_knowledge",
    "中台运营",
    "营销素材",
    "品牌规范",
    "品宣运营",
)
ACTIVE_TASK_STATUSES = {"pending", "in_progress", "blocked", "review_required"}
TERMINAL_CASE_STATUSES = {"archived", "cancelled"}


class CompanyCognitionHealthCheck:
    """Read-only health check for the company cognition layer."""

    def __init__(self, project_root: str | Path) -> None:
        self.project_root = Path(project_root)
        self.data_dir = self.project_root / "data"

    async def build_report(self) -> CompanyCognitionReport:
        components = {
            "employees": await self._employee_component(),
            "knowledge_base": await self._knowledge_base_component(),
            "harness": await self._harness_component(),
            "cases": await self._case_component(),
            "ai_inbox": await self._inbox_component(),
            "memory_matrix": await MemoryMatrixHealthCheck(
                self.project_root
            ).build_component(),
        }
        coverage = self._build_coverage(components)
        risks = self._build_risks(components, coverage)
        recommendations = self._build_recommendations(risks)
        return CompanyCognitionReport(
            generated_at=self._utcnow(),
            verdict=self._verdict(risks, components),
            components=components,
            coverage=coverage,
            risks=risks,
            recommendations=recommendations,
        )

    async def _employee_component(self) -> dict[str, Any]:
        path = self.data_dir / "employees.db"
        if not path.exists():
            return {"available": False, "path": str(path)}
        try:
            async with aiosqlite.connect(path) as db:
                db.row_factory = aiosqlite.Row
                await db.execute("PRAGMA busy_timeout=1000")
                if not await self._table_exists(db, "employees"):
                    return {"available": False, "path": str(path)}
                total = await self._count(db, "employees")
                memories = (
                    await self._count(db, "employee_memories")
                    if await self._table_exists(db, "employee_memories")
                    else 0
                )
                injections = (
                    await self._count(db, "employee_context_injections")
                    if await self._table_exists(db, "employee_context_injections")
                    else 0
                )
                identified = await self._count_where(
                    db,
                    "employees",
                    "trim(coalesce(display_name, '')) <> ''",
                )
                stable_address = await self._count_where(
                    db,
                    "employees",
                    "trim(coalesce(preferred_address, '')) not in ('', '同事')",
                )
                unknown_relation = await self._count_where(
                    db,
                    "employees",
                    "coalesce(relation_type, '') in ('', 'unknown')",
                )
                relation_counts = await self._group_counts(
                    db, "employees", "relation_type"
                )
                memory_kind_counts = (
                    await self._group_counts(db, "employee_memories", "kind")
                    if memories
                    else {}
                )
                memory_ready = 0
                if memories:
                    cursor = await db.execute("""
                        SELECT count(DISTINCT e.open_id)
                        FROM employees e
                        JOIN employee_memories m ON e.open_id = m.open_id
                        WHERE m.relevance >= 0.3
                        AND m.kind <> 'persona_evidence'
                    """)
                    row = await cursor.fetchone()
                    memory_ready = int(row[0] or 0)
        except Exception as exc:  # noqa: BLE001
            return {"available": False, "path": str(path), "error": str(exc)}
        return {
            "available": True,
            "path": str(path),
            "total": total,
            "identified": identified,
            "anonymous": max(total - identified, 0),
            "stable_address": stable_address,
            "unknown_relation": unknown_relation,
            "memory_ready": memory_ready,
            "memories": memories,
            "context_injections": injections,
            "relation_counts": relation_counts,
            "memory_kind_counts": memory_kind_counts,
        }

    async def _knowledge_base_component(self) -> dict[str, Any]:
        path = self.data_dir / "knowledge_base" / "kb.db"
        if not path.exists():
            return {"available": False, "path": str(path)}
        try:
            async with aiosqlite.connect(path) as db:
                db.row_factory = aiosqlite.Row
                await db.execute("PRAGMA busy_timeout=1000")
                if not await self._table_exists(db, "knowledge_bases"):
                    return {"available": False, "path": str(path)}
                cursor = await db.execute("""
                    SELECT
                        k.kb_id,
                        k.kb_name,
                        coalesce(k.doc_count, 0) AS declared_docs,
                        coalesce(k.chunk_count, 0) AS declared_chunks,
                        count(d.doc_id) AS actual_docs,
                        coalesce(sum(d.chunk_count), 0) AS actual_chunks,
                        coalesce(sum(d.media_count), 0) AS actual_media
                    FROM knowledge_bases k
                    LEFT JOIN kb_documents d ON k.kb_id = d.kb_id
                    GROUP BY k.kb_id, k.kb_name
                    ORDER BY actual_docs DESC, k.kb_name ASC
                """)
                rows = await cursor.fetchall()
        except Exception as exc:  # noqa: BLE001
            return {"available": False, "path": str(path), "error": str(exc)}

        knowledge_bases = [
            {
                "kb_id": row["kb_id"],
                "kb_name": row["kb_name"],
                "declared_docs": int(row["declared_docs"] or 0),
                "declared_chunks": int(row["declared_chunks"] or 0),
                "actual_docs": int(row["actual_docs"] or 0),
                "actual_chunks": int(row["actual_chunks"] or 0),
                "actual_media": int(row["actual_media"] or 0),
            }
            for row in rows
        ]
        by_name = {item["kb_name"]: item for item in knowledge_bases}
        missing_expected = [name for name in EXPECTED_KB_NAMES if name not in by_name]
        empty_expected = [
            name
            for name in EXPECTED_KB_NAMES
            if name in by_name and by_name[name]["actual_chunks"] <= 0
        ]
        return {
            "available": True,
            "path": str(path),
            "total": len(knowledge_bases),
            "nonempty": sum(1 for item in knowledge_bases if item["actual_chunks"] > 0),
            "documents": sum(item["actual_docs"] for item in knowledge_bases),
            "chunks": sum(item["actual_chunks"] for item in knowledge_bases),
            "media": sum(item["actual_media"] for item in knowledge_bases),
            "expected_names": list(EXPECTED_KB_NAMES),
            "missing_expected": missing_expected,
            "empty_expected": empty_expected,
            "knowledge_bases": knowledge_bases,
        }

    async def _harness_component(self) -> dict[str, Any]:
        path = self.data_dir / "harness.db"
        memory_path = self.data_dir / "harness_memory.db"
        if not path.exists():
            return {"available": False, "path": str(path)}
        try:
            async with aiosqlite.connect(path) as db:
                db.row_factory = aiosqlite.Row
                await db.execute("PRAGMA busy_timeout=1000")
                if not await self._table_exists(db, "harness_tasks"):
                    return {"available": False, "path": str(path)}
                total = await self._count(db, "harness_tasks")
                status_counts = await self._group_counts(db, "harness_tasks", "status")
                domain_counts = await self._group_counts(db, "harness_tasks", "domain")
                requester_count = await self._count_where(
                    db,
                    "harness_tasks",
                    "json_extract(payload_json, '$.requester_open_id') IS NOT NULL "
                    "AND trim(json_extract(payload_json, '$.requester_open_id')) <> ''",
                )
                cursor = await db.execute("SELECT task_id, status FROM harness_tasks")
                task_rows = await cursor.fetchall()
        except Exception as exc:  # noqa: BLE001
            return {"available": False, "path": str(path), "error": str(exc)}

        task_ids = {row["task_id"] for row in task_rows}
        completed_ids = {
            row["task_id"] for row in task_rows if row["status"] == "completed"
        }
        active = sum(
            count
            for status, count in status_counts.items()
            if status in ACTIVE_TASK_STATUSES
        )
        memory_task_ids: set[str] = set()
        memory_count = 0
        if memory_path.exists():
            try:
                async with aiosqlite.connect(memory_path) as db:
                    db.row_factory = aiosqlite.Row
                    await db.execute("PRAGMA busy_timeout=1000")
                    if await self._table_exists(db, "harness_memories"):
                        memory_count = await self._count(db, "harness_memories")
                        cursor = await db.execute(
                            "SELECT DISTINCT task_id FROM harness_memories"
                        )
                        memory_task_ids = {
                            str(row["task_id"]) for row in await cursor.fetchall()
                        }
            except Exception:
                memory_task_ids = set()
        completed_with_memory = len(completed_ids & memory_task_ids)
        orphaned_memories = len(memory_task_ids - task_ids)
        return {
            "available": True,
            "path": str(path),
            "memory_path": str(memory_path),
            "total": total,
            "active": active,
            "completed": status_counts.get("completed", 0),
            "requester_count": requester_count,
            "status_counts": status_counts,
            "domain_counts": domain_counts,
            "memory_count": memory_count,
            "completed_with_memory": completed_with_memory,
            "orphaned_memory_task_ids": orphaned_memories,
        }

    async def _case_component(self) -> dict[str, Any]:
        path = self.data_dir / "cases.db"
        if not path.exists():
            return {"available": False, "path": str(path)}
        try:
            async with aiosqlite.connect(path) as db:
                db.row_factory = aiosqlite.Row
                await db.execute("PRAGMA busy_timeout=1000")
                if not await self._table_exists(db, "cases"):
                    return {"available": False, "path": str(path)}
                total = await self._count(db, "cases")
                status_counts = await self._group_counts(db, "cases", "status")
                with_tasks = await self._count_where(
                    db,
                    "cases",
                    "task_ids_json IS NOT NULL AND task_ids_json <> '[]'",
                )
                with_requester = await self._count_where(
                    db,
                    "cases",
                    "json_extract(payload_json, '$.requester_open_id') IS NOT NULL "
                    "AND trim(json_extract(payload_json, '$.requester_open_id')) <> ''",
                )
                event_count = (
                    await self._count(db, "case_events")
                    if await self._table_exists(db, "case_events")
                    else 0
                )
        except Exception as exc:  # noqa: BLE001
            return {"available": False, "path": str(path), "error": str(exc)}
        active = sum(
            count
            for status, count in status_counts.items()
            if status not in TERMINAL_CASE_STATUSES
        )
        return {
            "available": True,
            "path": str(path),
            "total": total,
            "active": active,
            "with_tasks": with_tasks,
            "with_requester": with_requester,
            "event_count": event_count,
            "status_counts": status_counts,
        }

    async def _inbox_component(self) -> dict[str, Any]:
        path = self.data_dir / "ai_inbox.db"
        if not path.exists():
            return {"available": False, "path": str(path)}
        try:
            async with aiosqlite.connect(path) as db:
                db.row_factory = aiosqlite.Row
                await db.execute("PRAGMA busy_timeout=1000")
                if not await self._table_exists(db, "inbox_items"):
                    return {"available": False, "path": str(path)}
                total = await self._count(db, "inbox_items")
                status_counts = await self._group_counts(db, "inbox_items", "status")
                category_counts = await self._group_counts(
                    db, "inbox_items", "category"
                )
                with_case = await self._count_where(
                    db,
                    "inbox_items",
                    "trim(coalesce(case_id, '')) <> ''",
                )
                with_task = await self._count_where(
                    db,
                    "inbox_items",
                    "trim(coalesce(task_id, '')) <> ''",
                )
                event_count = (
                    await self._count(db, "inbox_events")
                    if await self._table_exists(db, "inbox_events")
                    else 0
                )
        except Exception as exc:  # noqa: BLE001
            return {"available": False, "path": str(path), "error": str(exc)}
        open_count = sum(
            status_counts.get(status, 0)
            for status in ("new", "acknowledged", "waiting_materials", "in_progress")
        )
        return {
            "available": True,
            "path": str(path),
            "total": total,
            "open": open_count,
            "with_case": with_case,
            "with_task": with_task,
            "event_count": event_count,
            "status_counts": status_counts,
            "category_counts": category_counts,
        }

    def _build_coverage(self, components: dict[str, Any]) -> dict[str, float | None]:
        employees = components["employees"]
        kb = components["knowledge_base"]
        harness = components["harness"]
        cases = components["cases"]
        inbox = components["ai_inbox"]
        memory_matrix = components["memory_matrix"]
        return {
            "employee_identity": self._ratio(
                employees.get("identified", 0), employees.get("total", 0)
            ),
            "employee_stable_address": self._ratio(
                employees.get("stable_address", 0), employees.get("total", 0)
            ),
            "employee_memory": self._ratio(
                employees.get("memory_ready", 0), employees.get("total", 0)
            ),
            "kb_nonempty": self._ratio(kb.get("nonempty", 0), kb.get("total", 0)),
            "harness_requester": self._ratio(
                harness.get("requester_count", 0), harness.get("total", 0)
            ),
            "harness_completed_memory": self._ratio(
                harness.get("completed_with_memory", 0), harness.get("completed", 0)
            ),
            "case_task_linkage": self._ratio(
                cases.get("with_tasks", 0), cases.get("total", 0)
            ),
            "case_requester": self._ratio(
                cases.get("with_requester", 0), cases.get("total", 0)
            ),
            "inbox_case_linkage": self._ratio(
                inbox.get("with_case", 0), inbox.get("total", 0)
            ),
            "inbox_task_linkage": self._ratio(
                inbox.get("with_task", 0), inbox.get("total", 0)
            ),
            "memory_matrix_healthy": self._ratio(
                sum(
                    1
                    for layer in memory_matrix.get("layers", {}).values()
                    if layer.get("status") == "healthy"
                ),
                len(memory_matrix.get("layers", {})),
            ),
        }

    def _build_risks(
        self,
        components: dict[str, Any],
        coverage: dict[str, float | None],
    ) -> list[CognitionRisk]:
        risks: list[CognitionRisk] = []
        employees = components["employees"]
        kb = components["knowledge_base"]
        harness = components["harness"]
        cases = components["cases"]
        inbox = components["ai_inbox"]
        memory_matrix = components["memory_matrix"]

        if harness.get("total", 0) > 0 and cases.get("total", 0) == 0:
            risks.append(
                CognitionRisk(
                    severity="high",
                    area="case",
                    message="Harness 已经有任务记录，但 Case 聚合层没有承接任何事项。",
                    evidence={
                        "harness_tasks": harness.get("total", 0),
                        "cases": cases.get("total", 0),
                    },
                    recommendation="继续使用 AI Inbox 自动建 Case，并补一个历史 Harness 任务回填到 Case 的迁移工具。",
                )
            )
        if employees.get("total", 0) and (coverage["employee_identity"] or 0) < 0.8:
            risks.append(
                CognitionRisk(
                    severity="medium",
                    area="employees",
                    message="员工身份覆盖不足，系统仍会把一部分同事当匿名用户处理。",
                    evidence={
                        "identified": employees.get("identified", 0),
                        "total": employees.get("total", 0),
                    },
                    recommendation="通过飞书同步、/employees fix 或首次接待补齐姓名、部门、岗位。",
                )
            )
        if employees.get("total", 0) and (coverage["employee_memory"] or 0) < 0.5:
            risks.append(
                CognitionRisk(
                    severity="medium",
                    area="employees",
                    message="可注入长期员工记忆偏少，个性化理解还主要依赖短期上下文。",
                    evidence={
                        "memory_ready": employees.get("memory_ready", 0),
                        "total": employees.get("total", 0),
                    },
                    recommendation="把高价值纠偏、偏好、岗位技能沉淀进 employee_memories，并定期归档到知识库。",
                )
            )
        if kb.get("total", 0) and kb.get("empty_expected"):
            risks.append(
                CognitionRisk(
                    severity="medium",
                    area="knowledge_base",
                    message="部分预期知识库存在但没有可检索 chunk。",
                    evidence={"empty_expected": kb.get("empty_expected", [])},
                    recommendation="确认这些知识库是占位还是应摄入资料；空库不要配置进关键会话。",
                )
            )
        if harness.get("total", 0) and (coverage["harness_requester"] or 0) < 0.7:
            risks.append(
                CognitionRisk(
                    severity="medium",
                    area="harness",
                    message="较多 Harness 任务缺少 requester_open_id，任务结果难以回流到员工画像。",
                    evidence={
                        "requester_count": harness.get("requester_count", 0),
                        "total": harness.get("total", 0),
                    },
                    recommendation="所有任务创建入口统一写入 requester_open_id、department、role。",
                )
            )
        if harness.get("orphaned_memory_task_ids", 0) > 0:
            risks.append(
                CognitionRisk(
                    severity="low",
                    area="harness_memory",
                    message="Harness 记忆中有部分 task_id 不在当前任务库，可能来自旧库或迁移前数据。",
                    evidence={
                        "orphaned_memory_task_ids": harness["orphaned_memory_task_ids"]
                    },
                    recommendation="保留作为历史记忆可接受；后续可以做一次 task_id 来源标记或归档清洗。",
                )
            )
        if inbox.get("available") and inbox.get("total", 0) == 0:
            risks.append(
                CognitionRisk(
                    severity="info",
                    area="ai_inbox",
                    message="AI Inbox 已就绪，但还没有正式消息进入。",
                    evidence={"path": inbox.get("path")},
                    recommendation="用一条真实飞书请求跑通员工消息到 Inbox/Case/Harness 的全链路。",
                )
            )
        memory_verdict = memory_matrix.get("verdict")
        if memory_verdict in {"action_required", "needs_attention"}:
            partial_layers = [
                f"{layer_id}:{layer.get('status')}"
                for layer_id, layer in memory_matrix.get("layers", {}).items()
                if layer.get("status") != "healthy"
            ]
            severity = "medium" if memory_verdict == "action_required" else "low"
            risks.append(
                CognitionRisk(
                    severity=severity,
                    area="memory_matrix",
                    message="六层记忆矩阵仍有层级未形成完整闭环。",
                    evidence={"layers": partial_layers},
                    recommendation=(
                        "优先补 L4 全局写入护栏、L5 sessionmemory.md 固定笔记、"
                        "L6 AutoDream 门控与索引修剪。"
                    ),
                )
            )
        return risks

    def _build_recommendations(self, risks: list[CognitionRisk]) -> list[str]:
        recommendations: list[str] = []
        seen: set[str] = set()
        for risk in risks:
            if risk.recommendation and risk.recommendation not in seen:
                seen.add(risk.recommendation)
                recommendations.append(risk.recommendation)
        if not recommendations:
            recommendations.append(
                "保持现有链路运行，下一步关注 Dashboard 可视化和周期性健康检查。"
            )
        return recommendations

    def _verdict(self, risks: list[CognitionRisk], components: dict[str, Any]) -> str:
        if not any(component.get("available") for component in components.values()):
            return "no_data"
        severities = {risk.severity for risk in risks}
        if "high" in severities:
            return "action_required"
        if "medium" in severities:
            return "needs_attention"
        if "low" in severities or "info" in severities:
            return "needs_observation"
        return "healthy"

    async def _table_exists(self, db: aiosqlite.Connection, table: str) -> bool:
        cursor = await db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        )
        return await cursor.fetchone() is not None

    async def _count(self, db: aiosqlite.Connection, table: str) -> int:
        cursor = await db.execute(f"SELECT count(*) FROM {table}")
        row = await cursor.fetchone()
        return int(row[0] or 0)

    async def _count_where(
        self,
        db: aiosqlite.Connection,
        table: str,
        where: str,
    ) -> int:
        cursor = await db.execute(f"SELECT count(*) FROM {table} WHERE {where}")
        row = await cursor.fetchone()
        return int(row[0] or 0)

    async def _group_counts(
        self,
        db: aiosqlite.Connection,
        table: str,
        column: str,
    ) -> dict[str, int]:
        cursor = await db.execute(
            f"""
            SELECT coalesce(nullif(trim({column}), ''), '(blank)') AS label,
                   count(*) AS count
            FROM {table}
            GROUP BY label
            ORDER BY count DESC
            """
        )
        rows = await cursor.fetchall()
        counter: Counter[str] = Counter()
        for row in rows:
            counter[str(row["label"])] = int(row["count"] or 0)
        return dict(counter)

    @staticmethod
    def _ratio(numerator: int, denominator: int) -> float | None:
        if denominator <= 0:
            return None
        return round(numerator / denominator, 4)

    @staticmethod
    def _utcnow() -> str:
        return datetime.now(timezone.utc).isoformat()


def dumps_report(report: CompanyCognitionReport) -> str:
    return json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True)
