"""Company cognition health plugin.

This plugin exposes a read-only health check that connects employee identity,
knowledge bases, Cases, Harness tasks, and AI Inbox into one operational view.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from dc_engines.company_cognition import (
    CaseBackfillReport,
    CompanyCognitionHealthCheck,
    CompanyCognitionReport,
    HistoricalCaseBackfiller,
)

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.star import Context, Star, register


@register(
    "company_cognition_plugin",
    "dc_agent",
    "公司认知层健康检查：员工/知识库/Case/Harness/Inbox 总账",
    "0.1.0",
)
class CompanyCognitionPlugin(Star):
    def __init__(self, context: Context) -> None:
        super().__init__(context)
        self.health: CompanyCognitionHealthCheck | None = None
        self.backfiller: HistoricalCaseBackfiller | None = None

    async def initialize(self) -> None:
        project_root = Path(__file__).resolve().parents[3]
        self.health = CompanyCognitionHealthCheck(project_root)
        self.backfiller = HistoricalCaseBackfiller(project_root / "data")
        self.context.company_cognition_health = self.health
        self.context.company_case_backfiller = self.backfiller
        try:
            self.context.register_web_api(
                "/company_cognition/health",
                self._api_health,
                ["GET"],
                "公司认知层健康检查",
            )
            self.context.register_web_api(
                "/company_cognition/backfill_cases",
                self._api_backfill_cases,
                ["GET"],
                "历史 Harness 任务回填 Case（dry-run）",
            )
            logger.info(
                "[company_cognition] API 已注册："
                "/api/plug/company_cognition/{health,backfill_cases}"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[company_cognition] 注册 API 失败：%s", exc)

    @filter.command("cognition", desc="公司认知层健康检查：/cognition health")
    async def cognition_command(self, event: AstrMessageEvent):
        text = (event.message_str or "").strip()
        if text.startswith("/cognition"):
            text = text[len("/cognition") :].strip()
        elif text.startswith("cognition"):
            text = text[len("cognition") :].strip()
        parts = text.split(maxsplit=1)
        sub = parts[0].lower() if parts else "health"
        rest = parts[1] if len(parts) > 1 else ""
        if sub in ("", "health", "check", "体检", "健康"):
            await self._handle_health(event)
            return
        if sub == "backfill":
            await self._handle_backfill(event, rest)
            return
        self._reply(
            event,
            "用法：\n  /cognition health\n  /cognition backfill cases [dry-run|apply]",
        )

    async def _handle_health(self, event: AstrMessageEvent) -> None:
        if self.health is None:
            self._reply(event, "公司认知层健康检查未初始化。")
            return
        report = await self.health.build_report()
        self._reply(event, self._format_report(report))

    async def _handle_backfill(self, event: AstrMessageEvent, rest: str) -> None:
        if self.backfiller is None:
            self._reply(event, "历史 Case 回填器未初始化。")
            return
        args = rest.strip().split()
        if not args or args[0].lower() != "cases":
            self._reply(event, "用法：/cognition backfill cases [dry-run|apply]")
            return
        mode = args[1].lower() if len(args) > 1 else "dry-run"
        if mode in ("dry-run", "dryrun", "plan", "preview"):
            report = await self.backfiller.plan()
        elif mode in ("apply", "run", "执行"):
            report = await self.backfiller.apply()
        else:
            self._reply(event, "用法：/cognition backfill cases [dry-run|apply]")
            return
        self._reply(event, self._format_backfill_report(report))

    async def _api_health(self, *args, **kwargs) -> dict[str, Any]:
        if self.health is None:
            return {
                "status": "error",
                "message": "health checker not ready",
                "data": None,
            }
        try:
            report = await self.health.build_report()
            return {"status": "ok", "message": None, "data": report.to_dict()}
        except Exception as exc:  # noqa: BLE001
            logger.warning("[company_cognition] health check failed: %s", exc)
            return {"status": "error", "message": str(exc), "data": None}

    async def _api_backfill_cases(self, *args, **kwargs) -> dict[str, Any]:
        if self.backfiller is None:
            return {
                "status": "error",
                "message": "case backfiller not ready",
                "data": None,
            }
        try:
            report = await self.backfiller.plan()
            return {"status": "ok", "message": None, "data": report.to_dict()}
        except Exception as exc:  # noqa: BLE001
            logger.warning("[company_cognition] backfill plan failed: %s", exc)
            return {"status": "error", "message": str(exc), "data": None}

    def _reply(self, event: AstrMessageEvent, text: str) -> None:
        event.set_result(MessageEventResult().message(text).use_t2i(False))

    def _format_report(self, report: CompanyCognitionReport) -> str:
        data = report.to_dict()
        components = data["components"]
        coverage = data["coverage"]

        def pct(key: str) -> str:
            value = coverage.get(key)
            if value is None:
                return "n/a"
            return f"{value * 100:.1f}%"

        employees = components["employees"]
        kb = components["knowledge_base"]
        harness = components["harness"]
        cases = components["cases"]
        inbox = components["ai_inbox"]
        memory_matrix = components["memory_matrix"]
        layers = memory_matrix.get("layers", {})
        layer_status = ", ".join(
            f"{layer_id}:{layer.get('status', 'unknown')}"
            for layer_id, layer in layers.items()
        )

        lines = [
            "公司认知层健康检查",
            f"- 结论: {data['verdict']}",
            (
                "- 员工: "
                f"{employees.get('total', 0)} 人；身份覆盖 {pct('employee_identity')}；"
                f"长期记忆覆盖 {pct('employee_memory')}"
            ),
            (
                "- 知识库: "
                f"{kb.get('total', 0)} 个；非空 {kb.get('nonempty', 0)} 个；"
                f"文档 {kb.get('documents', 0)}；chunks {kb.get('chunks', 0)}"
            ),
            (
                "- Harness: "
                f"{harness.get('total', 0)} 个任务；active {harness.get('active', 0)}；"
                f"requester 覆盖 {pct('harness_requester')}"
            ),
            (
                "- Case: "
                f"{cases.get('total', 0)} 个；带任务 {cases.get('with_tasks', 0)}；"
                f"requester 覆盖 {pct('case_requester')}"
            ),
            (
                "- AI Inbox: "
                f"{inbox.get('total', 0)} 条；open {inbox.get('open', 0)}；"
                f"Case 链接 {pct('inbox_case_linkage')}"
            ),
            (
                "- 记忆矩阵: "
                f"{memory_matrix.get('verdict', 'unknown')}；"
                f"健康层级 {pct('memory_matrix_healthy')}；{layer_status}"
            ),
        ]
        risks = data["risks"]
        if risks:
            lines.append("")
            lines.append("主要断点:")
            for risk in risks[:5]:
                lines.append(
                    f"- [{risk['severity']}] {risk['area']}: {risk['message']}"
                )
        recommendations = data["recommendations"]
        if recommendations:
            lines.append("")
            lines.append("下一步:")
            for item in recommendations[:5]:
                lines.append(f"- {item}")
        return "\n".join(lines)

    def _format_backfill_report(self, report: CaseBackfillReport) -> str:
        data = report.to_dict()
        mode = "dry-run" if data["dry_run"] else "apply"
        lines = [
            f"历史 Harness → Case 回填（{mode}）",
            f"- 计划 Case: {data['planned_group_count']}",
            f"- 覆盖任务: {data['planned_task_count']}",
            f"- 已跳过已挂 Case 任务: {data['skipped_tasks']}",
        ]
        if not data["dry_run"]:
            lines.append(f"- 创建: {data['created']}；更新: {data['updated']}")
        groups = data["groups"]
        if groups:
            lines.append("")
            lines.append("计划明细:")
            for group in groups[:8]:
                domains = ", ".join(group["domains"][:3]) or "-"
                requester = group.get("requester") or {}
                requester_label = (
                    requester.get("requester_display_name")
                    or requester.get("requester_open_id", "")[:8]
                    or "-"
                )
                lines.append(
                    f"- {group['action']} {group['case_id'][:12]} "
                    f"{group['task_count']} tasks [{domains}] {requester_label}"
                )
            if len(groups) > 8:
                lines.append(f"- ... 还有 {len(groups) - 8} 个分组")
        else:
            lines.append("- 没有需要回填的任务。")
        return "\n".join(lines)
