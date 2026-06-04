"""Department workflow trigger plugin.

This plugin only translates a matched department scenario into a Harness task.
It does not render Feishu cards and does not stop the normal LLM response path.
"""

from __future__ import annotations

from typing import Any

from dc_engines.department_workflows import (
    assemble_content_sop_source_context,
    build_content_sop_workflow_request,
    build_department_workflow_request,
    match_department_workflow,
    strip_internal_memory_context,
    workflow_catalog,
)
from dc_engines.harness.content_sop_runtime import plan_content_sop_dispatch

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register
from router.content_sop import infer_content_sop_metadata

_TRIGGER_KEYWORDS: tuple[str, ...] = (
    "部门工作流",
    "工作流任务",
    "训练任务",
    "测试任务",
    "按部门",
    "部门场景",
)
_INTERNAL_CONTEXT_MARKERS: tuple[str, ...] = ("<dc_agent_memory_context>",)


@register(
    "department_workflow_plugin",
    "dc_agent",
    "Department workflow matcher -> Harness task",
    "0.1.0",
)
class DepartmentWorkflowPlugin(Star):
    def __init__(self, context: Context, config=None) -> None:
        super().__init__(context, config)
        cfg = config or {}
        self.enabled = bool(cfg.get("enabled", False))
        self.dry_run = bool(cfg.get("dry_run", True))
        self.min_score = int(cfg.get("min_score", 12))
        self.group_min_score = int(cfg.get("group_min_score", 24))
        self.explicit_min_score = int(cfg.get("explicit_min_score", 10))
        self.create_tasks = bool(cfg.get("create_tasks", True))
        self.attach_case = bool(cfg.get("attach_case", True))
        self.notify_on_match = bool(cfg.get("notify_on_match", True))
        self.notify_in_dry_run = bool(cfg.get("notify_in_dry_run", False))

    async def initialize(self) -> None:
        try:
            self.context.register_web_api(
                "/department-workflows",
                self._api_department_workflows,
                ["GET"],
                "列出部门工作流注册表",
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[department_workflow] register web api failed: %s", exc)

    @filter.event_message_type(
        EventMessageType.GROUP_MESSAGE | EventMessageType.PRIVATE_MESSAGE
    )
    async def on_message(self, event: AstrMessageEvent):
        raw_text = (event.message_str or "").strip()
        if not raw_text:
            return
        text = strip_internal_memory_context(raw_text)
        if not text:
            return
        if not self.enabled:
            return

        is_group = "GroupMessage" in (event.unified_msg_origin or "") or "Group" in str(
            getattr(event.message_obj, "type", "")
        )
        if is_group and not getattr(event, "is_at_or_wake_command", False):
            return

        engine = getattr(self.context, "harness_engine", None)
        store = getattr(self.context, "harness_store", None)
        if not self.dry_run and (engine is None or store is None):
            return

        sender_id = _get_sender_id(event)
        requester_meta = await self._load_requester_meta(sender_id)
        department = str(requester_meta.get("requester_department") or "")
        role = str(requester_meta.get("requester_role") or "")
        relation_type = str(requester_meta.get("requester_relation_type") or "")
        explicit_trigger = any(keyword in text for keyword in _TRIGGER_KEYWORDS)

        match = match_department_workflow(
            employee_department=department,
            text=text,
            employee_role=role,
            relation_type=relation_type,
            min_score=self.explicit_min_score if explicit_trigger else self.min_score,
            allow_department_only=explicit_trigger,
        )
        if match is None:
            return
        if is_group and not explicit_trigger and match.score < self.group_min_score:
            return

        if self.dry_run:
            logger.info(
                "[department_workflow][dry-run] would create task umo=%s department=%s scenario=%s score=%s reasons=%s",
                event.unified_msg_origin,
                match.department_id,
                match.scenario_id,
                match.score,
                ",".join(match.reasons),
            )
            if self.notify_on_match and self.notify_in_dry_run:
                await self._send_boundary_notice(
                    event,
                    _format_boundary_notice(match=match, dry_run=True),
                )
            return

        try:
            conv_id = await self.context.conversation_manager.get_curr_conversation_id(
                event.unified_msg_origin
            )
            if not conv_id:
                conv_id = await self.context.conversation_manager.new_conversation(
                    event.unified_msg_origin,
                    event.get_platform_id(),
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[department_workflow] conversation lookup failed: %s", exc)
            return

        if await self._has_active_duplicate(
            store,
            conv_id,
            department_id=match.department_id,
            scenario_id=match.scenario_id,
        ):
            return

        try:
            req = self._build_harness_request(
                event,
                match=match,
                conversation_id=conv_id,
                message_text=text,
                requester_meta=requester_meta or {"requester_open_id": sender_id},
            )
            req.payload["auto_complete_on_response"] = True
            if not self.create_tasks:
                logger.info(
                    "[department_workflow] matched but create_tasks=false department=%s scenario=%s",
                    match.department_id,
                    match.scenario_id,
                )
                return
            task = await engine.create_task(req)
            event.set_extra("department_workflow_task_id", task.task_id)
            event.set_extra("department_workflow_domain", req.domain)
            await self._dispatch_ready_content_sop_task(event, task)
            link_task = getattr(self.context, "ai_inbox_link_task", None)
            if link_task is not None:
                await link_task(
                    event,
                    task.task_id,
                    source="department_workflow_plugin",
                )
            if self.notify_on_match:
                await self._send_boundary_notice(
                    event,
                    _format_boundary_notice(
                        match=match,
                        task_id=task.task_id,
                        payload=req.payload,
                    ),
                )
            logger.info(
                "[department_workflow] created task umo=%s department=%s scenario=%s task=%s",
                event.unified_msg_origin,
                match.department_id,
                match.scenario_id,
                task.task_id[:8],
            )
        except Exception:
            logger.warning("[department_workflow] create task failed", exc_info=True)
            return

        if self.attach_case:
            await self._attach_current_case(event, task.task_id)

    async def _dispatch_ready_content_sop_task(self, event: AstrMessageEvent, task):
        payload = task.payload or {}
        if payload.get("workflow_kind") != "content_sop_workflow":
            return
        decision = plan_content_sop_dispatch(task)
        engine = getattr(self.context, "harness_engine", None)
        if not decision.should_dispatch:
            if engine is not None:
                await engine.append_trace(
                    task.task_id,
                    "content_sop_material_intake_required",
                    {"reason": decision.reason},
                )
            return
        dispatch = getattr(self.context, "dispatch_task_to_hermes", None)
        if dispatch is None:
            if engine is not None:
                await engine.fail_task(
                    task.task_id,
                    reason="Hermes dispatcher unavailable for ready content SOP task",
                )
            return
        if engine is not None:
            await engine.mark_in_progress(task.task_id, note="content_sop_dispatch")
        hermes_payload = decision.hermes_payload or {}
        ok = await dispatch(
            task.task_id,
            "content_sop_workflow",
            str(hermes_payload.get("brief") or task.title),
            event.unified_msg_origin,
            {
                "content_sop": hermes_payload,
                "department_id": hermes_payload.get("department_id", ""),
                "scenario_id": hermes_payload.get("scenario_id", ""),
            },
        )
        if engine is not None:
            if ok:
                await engine.append_trace(
                    task.task_id,
                    "content_sop_dispatched_to_hermes",
                    {"dispatcher": "hermes_bridge"},
                )
            else:
                await engine.fail_task(
                    task.task_id,
                    reason="Hermes dispatcher returned false for content SOP task",
                )

    def _build_harness_request(
        self,
        event: AstrMessageEvent,
        *,
        match,
        conversation_id: str,
        message_text: str,
        requester_meta: dict[str, Any],
    ):
        content_meta = infer_content_sop_metadata(
            message_text,
            attachment_summary=_event_extra(event, "attachment_summary"),
            has_attachments=bool(_event_extra(event, "attachment_kinds")),
        )
        if content_meta.is_content_sop and match.department_id in {
            "client_dept",
            "planning",
            "brand_publicity",
        }:
            source_context = assemble_content_sop_source_context(
                _event_extra(event, "dc_agent_memory_context")
            )
            return build_content_sop_workflow_request(
                match,
                conversation_id=conversation_id,
                platform_id=event.get_platform_id(),
                session_id=event.unified_msg_origin,
                source="department_workflow_plugin",
                message_text=message_text,
                content_type=content_meta.content_type.value,
                requester_meta=requester_meta,
                knowledge_context=source_context.knowledge_context,
                source_citations=source_context.source_citations,
            )
        return build_department_workflow_request(
            match,
            conversation_id=conversation_id,
            platform_id=event.get_platform_id(),
            session_id=event.unified_msg_origin,
            source="department_workflow_plugin",
            message_text=message_text,
            requester_meta=requester_meta,
        )

    async def _send_boundary_notice(
        self,
        event: AstrMessageEvent,
        text: str,
    ) -> None:
        if not text:
            return
        try:
            await event.send(MessageChain([Plain(text)]))
        except Exception as exc:  # noqa: BLE001
            logger.debug("[department_workflow] boundary notice skipped: %s", exc)

    async def _api_department_workflows(self, _request) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "dry_run": self.dry_run,
            "workflow_count": len(workflow_catalog()),
            "workflows": workflow_catalog(),
        }

    async def _load_requester_meta(self, sender_id: str) -> dict[str, Any]:
        if not sender_id:
            return {}
        emp_store = getattr(self.context, "employee_store", None)
        if emp_store is None:
            return {"requester_open_id": sender_id}
        try:
            emp = await emp_store.get_employee(sender_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[department_workflow] employee lookup failed: %s", exc)
            return {"requester_open_id": sender_id}
        if emp is None:
            return {"requester_open_id": sender_id}
        return {
            "requester_open_id": emp.open_id,
            "requester_display_name": emp.display_name or "",
            "requester_department": emp.department or "",
            "requester_role": emp.role or "",
            "requester_relation_type": emp.relation_type or "",
        }

    async def _has_active_duplicate(
        self,
        store,
        conversation_id: str,
        *,
        department_id: str,
        scenario_id: str,
    ) -> bool:
        try:
            recent = await store.list_tasks_for_conversation(conversation_id, limit=8)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[department_workflow] duplicate lookup failed: %s", exc)
            return False
        for task in recent:
            if task.status not in ("pending", "in_progress"):
                continue
            if (
                task.payload.get("workflow_kind") == "department_workflow"
                and task.payload.get("department_id") == department_id
                and task.payload.get("scenario_id") == scenario_id
            ):
                return True
        return False

    async def _attach_current_case(
        self,
        event: AstrMessageEvent,
        task_id: str,
    ) -> None:
        case_engine = getattr(self.context, "case_engine", None)
        if case_engine is None:
            return
        try:
            case = await case_engine.get_current_case_for_session(
                event.unified_msg_origin
            )
            if case is None:
                ensure_case = getattr(self.context, "ai_inbox_ensure_case", None)
                if ensure_case is not None:
                    case_id = await ensure_case(
                        event,
                        category="request",
                        text=event.message_str or "",
                        task_id=task_id,
                    )
                    if case_id:
                        case = await case_engine.store.get_case(case_id)
            if case is not None:
                await case_engine.attach_task(case.case_id, task_id)
                link_task = getattr(self.context, "ai_inbox_link_task", None)
                if link_task is not None:
                    await link_task(
                        event,
                        task_id,
                        case_id=case.case_id,
                        source="department_workflow_plugin",
                    )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[department_workflow] case attach skipped: %s", exc)


def _get_sender_id(event: AstrMessageEvent) -> str:
    try:
        return str(event.get_sender_id() or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _event_extra(event: AstrMessageEvent, key: str):
    getter = getattr(event, "get_extra", None)
    if not callable(getter):
        return None
    try:
        return getter(key)
    except Exception:  # noqa: BLE001
        return None


def _format_boundary_notice(
    *,
    match,
    task_id: str = "",
    payload: dict[str, Any] | None = None,
    dry_run: bool = False,
) -> str:
    payload = payload or {}
    material_status = str(payload.get("material_status") or "")
    missing = payload.get("missing_required_inputs") or []
    missing_labels = [
        str(item.get("label") or item.get("key") or "")
        for item in missing
        if isinstance(item, dict)
    ]

    status_text = {
        "ready": "资料基本够，可以进入执行。",
        "partial": "已有部分资料，后续可能还要补充关键信息。",
        "needs_materials": "资料还不完整，涉及真实业务时我会优先提示补充。",
    }.get(material_status, "我会按这个场景进入任务台账。")
    task_text = "dry-run 观察中，暂不落任务" if dry_run else f"task #{task_id[:8]}"

    lines = [
        "已识别到部门工作流边界：",
        f"- 部门：{match.workflow.department_name}",
        f"- 场景：{match.scenario.name}",
        f"- 状态：{status_text}",
        f"- 记录：{task_text}",
    ]
    if missing_labels:
        lines.append(f"- 待补：{', '.join(missing_labels[:4])}")
    lines.append("我会继续正常回复，不会打断当前对话。")
    return "\n".join(lines)
