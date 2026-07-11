from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from dc_engines.harness import HARNESS_TRUTH_GUARD
from dc_engines.harness.loop_runtime import LoopOrchestrator, extract_evidence

logger = logging.getLogger(__name__)

MAX_TASKS_INJECT = 5
TRUTH_GUARD_PLATFORMS = {"巅池-Agent小助手", "巅池-技术（DevOps）", "巅池-技术"}
ACTIVE_STATUSES = ("pending", "in_progress", "blocked", "review_required")

HARD_GUARD_PREFIX = "\n\n## Harness 任务状态约束（必须遵守）\n"
HARD_GUARD_RULES = """
铁律（违反会被用户当作欺骗）：
- 上面列出的 task 是 Harness 真实在跑的，**不要假装"已完成""已分析"\
"已起草"**。
- 用户问『xxx 做完了吗？』时，**必须如实**答状态（如"还在 in_progress, \
Hermes 处理中"），不要编。
- task 状态是 **failed** 时，必须告知用户失败 + 建议重试，**不要**编"已完成"。
- 如果没有任何 active task，本约束自动失效，你正常对话即可。
- 若用户的请求需要 Harness 处理但目前没有 task，告诉用户「我会安排 \
Harness 处理」而不是直接假装做了。
"""

TRUTH_INTAKE_SOURCE = "llm_router_truth_intake"
CASUAL_ROUTER_INTENTS = {"casual", "fallback", "local_casual_ack"}
TASK_CONTEXT_KEYWORDS = (
    "任务",
    "task",
    "harness",
    "Hermes",
    "进度",
    "状态",
    "完成",
    "做完",
    "blocked",
    "in_progress",
    "pending",
    "灰度",
    "验证",
)

LLM_ERROR_PATTERNS: tuple[str, ...] = (
    "All chat models failed",
    "BadRequestError",
    "RateLimitError",
    "AuthenticationError",
    "APIConnectionError",
    "Connection error",
    "Error code: 4",
    "Error code: 5",
)
INSUFFICIENT_MATERIAL_PATTERNS: tuple[str, ...] = (
    "资料不足",
    "材料不足",
    "素材不足",
    "缺少资料",
    "缺少材料",
    "缺少素材",
    "还需要",
    "需要补充",
    "无法确认",
    "不能确认",
    "无法核实",
    "无法验证",
    "不能完成",
    "无法完成",
    "未命中",
    "没有命中",
    "检索未命中",
    "未找到相关资料",
    "没有找到相关资料",
    "没找到相关资料",
    "无来源依据",
    "缺少来源",
    "缺少引用",
    "白名单未配置",
    "资料库未配置",
    "待审核",
    "等待审核",
    "待员工确认",
    "等待员工确认",
    "没读到",
    "未读取到",
    "没有读取到",
    "未提供",
    "没有提供",
    "insufficient material",
    "missing material",
    "missing required input",
    "need more information",
    "insufficient information",
    "not enough information",
    "cannot verify",
    "unable to verify",
    "no matches",
    "no relevant documents",
    "no source",
    "no citation",
    "requires review",
    "pending review",
    "waiting for approval",
)
INSUFFICIENT_MATERIAL_SUCCESS_EXCEPTIONS: tuple[str, ...] = (
    "未完成项：无",
    "未完成项: 无",
    "无需补充",
    "不需要补充",
    "无需提供",
    "not found fallback",
    "fixed the not found",
)
NONTERMINAL_ACK_PREFIXES: tuple[str, ...] = (
    "已进入生图任务",
    "已进入图片转视频任务",
    "已进入文生视频任务",
    "任务已进入队列",
    "任务已创建，正在处理",
    "请求已接收，正在处理",
)

MD_HEADER_RE = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
MD_QUOTE_RE = re.compile(r"^\s{0,3}>\s*", re.MULTILINE)
MD_BOLD_RE = re.compile(r"\*\*([^*\n]+)\*\*")
MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")

HARNESS_TERMINAL_STATUSES = {"completed", "cancelled", "failed"}
AUTO_COMPLETE_TASK_ID_KEYS = (
    "department_workflow_task_id",
    "dc_truth_intake_task_id",
    "workflow_intent_task_id",
)
AUTO_COMPLETE_SOURCE_DEFAULTS = {
    "department_workflow_plugin",
    "llm_router_truth_intake",
    "dc_router_truth_intake",
    "workflow_intent_plugin",
}


def safe_extra(event: Any, key: str) -> str:
    getter = getattr(event, "get_extra", None)
    if not callable(getter):
        return ""
    try:
        return str(getter(key) or "")
    except Exception:  # noqa: BLE001
        return ""


def should_inject_active_tasks(event: Any, text: str) -> bool:
    router_intent = (
        safe_extra(event, "dc_router_intent") or safe_extra(event, "llm_router_intent")
    ).strip()
    text_lower = text.lower()
    if router_intent in CASUAL_ROUTER_INTENTS:
        return any(keyword.lower() in text_lower for keyword in TASK_CONTEXT_KEYWORDS)
    if router_intent in {"truth_intake", "task", "devops", "workflow"}:
        return True
    if text.startswith("#"):
        return False
    return any(keyword.lower() in text_lower for keyword in TASK_CONTEXT_KEYWORDS)


def format_task_line(task: Any) -> str:
    payload = getattr(task, "payload", {}) or {}
    workflow = payload.get("workflow_kind") or task.domain or "general"
    brief = (payload.get("brief") or task.title or "").strip()
    if len(brief) > 80:
        brief = brief[:80] + "..."
    line = (
        f"- task_id={task.task_id[:8]}  status={task.status}  "
        f"workflow={workflow}  created={task.created_at}\n"
        f"  brief: {brief}"
    )
    if payload.get("source") != TRUTH_INTAKE_SOURCE:
        return line

    archive_dir = str(payload.get("archive_dir") or "").strip()
    attachments = payload.get("attachments") or []
    material_lines: list[str] = []
    if archive_dir:
        material_lines.append(f"  source_archive_dir: {archive_dir}")
    for item in attachments[:5]:
        stored_path = str(item.get("stored_path") or "").strip()
        original_name = str(item.get("original_name") or "").strip()
        if not stored_path:
            continue
        suffix = Path(stored_path).suffix or "file"
        label = original_name or Path(stored_path).name
        material_lines.append(
            f"  source_attachment: {label} ({suffix}) -> {stored_path}"
        )
    if material_lines:
        material_lines.append(
            "  source_rule: 优先读取上述 source_attachment / source_archive_dir；"
            "不要把 data/temp 里的历史导入、旧日志或无关索引当作本次素材。"
        )
        line += "\n" + "\n".join(material_lines)
    return line


class HarnessStateInjectionRuntime:
    def __init__(self, context: Any) -> None:
        self.context = context

    async def inject_active_tasks(self, event: Any, req: Any) -> None:
        text = (getattr(event, "message_str", "") or "").strip()
        platform_id = ""
        try:
            platform_id = event.get_platform_id() or ""
        except Exception:  # noqa: BLE001
            platform_id = ""

        original = getattr(req, "system_prompt", None) or ""
        if platform_id in TRUTH_GUARD_PLATFORMS:
            original += HARNESS_TRUTH_GUARD.rstrip()

        store = getattr(self.context, "harness_store", None)
        if store is None:
            req.system_prompt = original
            return

        umo = getattr(event, "unified_msg_origin", None)
        if not umo:
            req.system_prompt = original
            return

        if not should_inject_active_tasks(event, text):
            req.system_prompt = original
            return

        try:
            active = await store.list_tasks_for_session(
                umo,
                limit=MAX_TASKS_INJECT,
                statuses=ACTIVE_STATUSES,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[harness_state_injector] list_tasks_for_session failed: %s", exc
            )
            req.system_prompt = original
            return

        if not active:
            req.system_prompt = original
            return

        task_lines = "\n".join(format_task_line(task) for task in active)
        injection = HARD_GUARD_PREFIX + task_lines + "\n" + HARD_GUARD_RULES.rstrip()
        req.system_prompt = original + injection

        logger.info(
            "[harness_state_injector] injected %d active task constraints -> umo=%s",
            len(active),
            str(umo)[:80],
        )

    _format_task_line = staticmethod(format_task_line)
    _safe_extra = staticmethod(safe_extra)
    _should_inject_active_tasks = staticmethod(should_inject_active_tasks)


def classify_response_quality(resp: Any, text: str) -> str:
    if getattr(resp, "role", None) == "err":
        return "error"
    head = text[:400]
    for pattern in LLM_ERROR_PATTERNS:
        if pattern in head:
            return "error"
    lowered = head.lower()
    for exception in INSUFFICIENT_MATERIAL_SUCCESS_EXCEPTIONS:
        if exception.lower() in lowered:
            return "success"
    for pattern in INSUFFICIENT_MATERIAL_PATTERNS:
        if pattern.lower() in lowered:
            return "insufficient_materials"
    if any(head.strip().startswith(prefix) for prefix in NONTERMINAL_ACK_PREFIXES):
        return "acknowledged"
    return "success"


def extract_summary(text: str, max_len: int = 200) -> str:
    cleaned = MD_LINK_RE.sub(r"\1", text)
    cleaned = MD_BOLD_RE.sub(r"\1", cleaned)
    cleaned = MD_HEADER_RE.sub("", cleaned)
    cleaned = MD_QUOTE_RE.sub("", cleaned)
    cleaned = cleaned.strip()
    paragraphs = [p.strip() for p in cleaned.split("\n\n") if p.strip()]
    candidate = paragraphs[0] if paragraphs else cleaned
    if len(candidate) < 20 and len(paragraphs) >= 2:
        candidate = paragraphs[1]
    return candidate[:max_len]


def event_task_ids(event: Any) -> list[str]:
    seen: set[str] = set()
    task_ids: list[str] = []
    for key in AUTO_COMPLETE_TASK_ID_KEYS:
        raw_value = event.get_extra(key)
        values = raw_value if isinstance(raw_value, (list, tuple, set)) else [raw_value]
        for value in values:
            if not isinstance(value, str):
                continue
            task_id = value.strip()
            if task_id and task_id not in seen:
                seen.add(task_id)
                task_ids.append(task_id)
    return task_ids


def result_plain_text(event: Any) -> str:
    result = event.get_result()
    if result is None or not result.chain:
        return ""
    parts: list[str] = []
    for comp in result.chain:
        text = getattr(comp, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts).strip()


def allows_auto_complete(task: Any) -> bool:
    payload = getattr(task, "payload", {}) or {}
    if payload.get("review_required_by_default") is True:
        return False
    if payload.get("generation_allowed") is False:
        return False
    if payload.get("missing_required_inputs"):
        return False
    explicit = payload.get("auto_complete_on_response")
    if explicit is not None:
        return bool(explicit)
    return payload.get("source") in AUTO_COMPLETE_SOURCE_DEFAULTS


class HarnessSensorRuntime:
    def __init__(self, context: Any) -> None:
        self.context = context

    async def settle_llm_response(self, event: Any, resp: Any) -> None:
        if not resp or not (getattr(resp, "completion_text", "") or "").strip():
            return
        text = (resp.completion_text or "").strip()
        await self.settle_active_tasks(
            event,
            text=text,
            quality=classify_response_quality(resp, text),
            source="harness_sensor_plugin",
            role=getattr(resp, "role", None),
        )

    async def settle_plugin_result(self, event: Any) -> bool:
        result = event.get_result()
        if result is None or result.is_model_result():
            return False
        text = result_plain_text(event)
        if not text:
            logger.debug(
                "[harness_sensor] maybe_complete_plugin_result skipped because result plain text is empty."
            )
            return False
        await self.settle_active_tasks(
            event,
            text=text,
            quality=classify_response_quality(None, text),
            source="harness_sensor_plugin:decorating_result",
            role=None,
            allowed_statuses={"pending", "in_progress"},
        )
        return True

    async def settle_active_tasks(
        self,
        event: Any,
        *,
        text: str,
        quality: str,
        source: str,
        role: str | None,
        allowed_statuses: set[str] | None = None,
    ) -> None:
        harness_engine = getattr(self.context, "harness_engine", None)
        if harness_engine is None:
            return

        tasks = await self.load_target_tasks(
            event,
            harness_engine,
            allowed_statuses=allowed_statuses,
        )
        if not tasks:
            return

        if quality == "error":
            for task in tasks:
                try:
                    await self._record_loop_response_observation(
                        harness_engine,
                        task,
                        text=text,
                        quality=quality,
                        source=source,
                    )
                    await harness_engine.fail_task(task.task_id, reason=text[:200])
                    logger.info(
                        "[harness_sensor] Task %s marked failed (LLM error)",
                        task.task_id[:8],
                    )
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "[harness_sensor] fail_task %s failed",
                        task.task_id,
                        exc_info=True,
                    )
            return

        if quality == "insufficient_materials":
            for task in tasks:
                try:
                    await self._record_loop_response_observation(
                        harness_engine,
                        task,
                        text=text,
                        quality=quality,
                        source=source,
                    )
                    await harness_engine.set_status(
                        task.task_id,
                        "blocked",
                        event_payload={
                            "reason": "insufficient_source_materials",
                            "response_preview": text[:500],
                            "source": source,
                        },
                    )
                    await self._update_inbox(
                        task.task_id,
                        status="waiting_materials",
                        event_type="task_waiting_materials",
                        source=source,
                    )
                    logger.info(
                        "[harness_sensor] Task %s blocked (insufficient materials)",
                        task.task_id[:8],
                    )
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "[harness_sensor] block_task %s failed",
                        task.task_id,
                        exc_info=True,
                    )
            return

        if quality == "acknowledged":
            for task in tasks:
                try:
                    await self._record_loop_response_observation(
                        harness_engine,
                        task,
                        text=text,
                        quality=quality,
                        source=source,
                    )
                    await self._update_inbox(
                        task.task_id,
                        status="in_progress",
                        event_type="task_acknowledged",
                        source=source,
                    )
                    logger.debug(
                        "[harness_sensor] Task %s remains active after acknowledgement",
                        task.task_id[:8],
                    )
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "[harness_sensor] acknowledge task %s failed",
                        task.task_id,
                        exc_info=True,
                    )
            return

        summary = extract_summary(text)
        for task in tasks:
            try:
                result = {
                    "summary": summary,
                    "response_preview": text[:500],
                    "source": source,
                    "quality": quality,
                    "role": role,
                    "evidence": [
                        {
                            "type": "assistant_response",
                            "source": source,
                            "preview": text[:500],
                        }
                    ],
                }
                await self._record_loop_response_observation(
                    harness_engine,
                    task,
                    text=text,
                    quality=quality,
                    source=source,
                    result=result,
                )
                if (getattr(task, "payload", {}) or {}).get(
                    "review_required_by_default"
                ) is True:
                    await harness_engine.mark_review_required(
                        task.task_id,
                        reviewer_note="review_required_by_default",
                        result=result,
                    )
                    inbox_status = "waiting_review"
                    inbox_event = "task_waiting_review"
                    log_message = "review_required"
                else:
                    await harness_engine.complete_task(
                        task.task_id,
                        result=result,
                    )
                    inbox_status = "delivered"
                    inbox_event = "task_response_delivered"
                    log_message = "completed"
                await self._update_inbox(
                    task.task_id,
                    status=inbox_status,
                    event_type=inbox_event,
                    source=source,
                )
                logger.debug(
                    "[harness_sensor] Task %s %s (quality=%s, %d-char summary)",
                    task.task_id[:8],
                    log_message,
                    quality,
                    len(summary),
                )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "[harness_sensor] settle task %s failed",
                    task.task_id,
                    exc_info=True,
                )

    async def load_target_tasks(
        self,
        event: Any,
        harness_engine: Any,
        *,
        allowed_statuses: set[str] | None = None,
    ) -> list[Any]:
        store = getattr(harness_engine, "store", None)
        if store is None:
            return []

        event_ids = event_task_ids(event)
        if not event_ids:
            return []

        tasks: list[Any] = []
        for task_id in event_ids:
            try:
                task = await store.get_task(task_id)
            except Exception:  # noqa: BLE001
                task = None
            if self._is_eligible(task, allowed_statuses=allowed_statuses):
                tasks.append(task)
        return tasks

    async def _update_inbox(
        self,
        task_id: str,
        *,
        status: str,
        event_type: str,
        source: str,
    ) -> None:
        inbox_store = getattr(self.context, "ai_inbox_store", None)
        if inbox_store is not None:
            try:
                item = await inbox_store.find_by_task_id(task_id)
                if item is not None:
                    await inbox_store.update_item(
                        item.item_id,
                        status=status,
                        event_type=event_type,
                        event_payload={"source": source},
                    )
            except Exception as exc:  # noqa: BLE001
                logger.debug("[harness_sensor] inbox update skipped: %s", exc)
        update_insight_task = getattr(
            self.context,
            "employee_insight_update_task",
            None,
        )
        if callable(update_insight_task):
            try:
                await update_insight_task(task_id, status=status, source=source)
            except Exception as exc:  # noqa: BLE001
                logger.debug("[harness_sensor] insight update skipped: %s", exc)

    async def _record_loop_response_observation(
        self,
        harness_engine: Any,
        task: Any,
        *,
        text: str,
        quality: str,
        source: str,
        result: dict[str, Any] | None = None,
    ) -> None:
        store = getattr(harness_engine, "store", None)
        if store is None:
            return
        orchestrator = LoopOrchestrator(store)
        try:
            await orchestrator.record_action_started(
                task.task_id,
                step_id=f"sensor:{source}:response",
                summary="Classify assistant response for harness settlement.",
                metadata={"source": source},
            )
            await orchestrator.record_observation(
                task.task_id,
                step_id=f"sensor:{source}:observation",
                summary=extract_summary(text),
                evidence=extract_evidence(result or {"response_preview": text[:500]}),
                metadata={"quality": quality, "source": source},
            )
            await orchestrator.record_decision(
                task.task_id,
                step_id=f"sensor:{source}:decision",
                summary=f"Response quality classified as {quality}.",
                status="ok" if quality == "success" else "blocked",
                metadata={"quality": quality, "source": source},
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[harness_sensor] loop observation skipped: %s", exc)

    @staticmethod
    def _is_eligible(task: Any, *, allowed_statuses: set[str] | None = None) -> bool:
        payload = getattr(task, "payload", {}) or {}
        can_settle = payload.get(
            "review_required_by_default"
        ) is True or allows_auto_complete(task)
        return (
            task is not None
            and task.status not in HARNESS_TERMINAL_STATUSES
            and task.status != "review_required"
            and can_settle
            and (allowed_statuses is None or task.status in allowed_statuses)
        )

    _settle_active_tasks = settle_active_tasks
    _load_target_tasks = load_target_tasks
