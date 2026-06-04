"""W0 Phase 0.2 Sensor 硬化 Star 插件（重装移植版）。

旧版直接改 ``astrbot/core/pipeline/.../internal.py`` 的 ``_maybe_complete_harness_task``，
新架构改用 ``@filter.on_llm_response`` 钩子，零核心文件修改。

职责：LLM 响应完成后，检测错误模式 → 路由到 ``fail_task``（避免错误被晋升为长期记忆），
资料不足 → 回到 ``blocked``，正常响应 → ``complete_task`` 附带 quality=success 标记。
"""

from __future__ import annotations

import re

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.provider import LLMResponse
from astrbot.api.star import Context, Star, register

# LLM 错误响应特征（来自 W0 Phase 0.2 实测）
_LLM_ERROR_PATTERNS: tuple[str, ...] = (
    "All chat models failed",
    "BadRequestError",
    "RateLimitError",
    "AuthenticationError",
    "APIConnectionError",
    "Connection error",
    "Error code: 4",
    "Error code: 5",
)
_INSUFFICIENT_MATERIAL_PATTERNS: tuple[str, ...] = (
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
    "没读到",
    "未读取到",
    "没有读取到",
    "未提供",
    "没有提供",
    "insufficient material",
    "missing material",
    "need more information",
    "cannot verify",
    "unable to verify",
)

_MD_HEADER_RE = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_MD_QUOTE_RE = re.compile(r"^\s{0,3}>\s*", re.MULTILINE)
_MD_BOLD_RE = re.compile(r"\*\*([^*\n]+)\*\*")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")

# Harness 终态（不应再被 sensor 修改）
_HARNESS_TERMINAL_STATUSES = {"completed", "cancelled", "failed"}
_AUTO_COMPLETE_TASK_ID_KEYS = (
    "department_workflow_task_id",
    "dc_truth_intake_task_id",
    "workflow_intent_task_id",
)

_AUTO_COMPLETE_SOURCE_DEFAULTS = {
    "department_workflow_plugin",
    "llm_router_truth_intake",
    "workflow_intent_plugin",
}


def _classify_response_quality(resp: LLMResponse, text: str) -> str:
    if getattr(resp, "role", None) == "err":
        return "error"
    head = text[:400]
    for pattern in _LLM_ERROR_PATTERNS:
        if pattern in head:
            return "error"
    lowered = head.lower()
    for pattern in _INSUFFICIENT_MATERIAL_PATTERNS:
        if pattern.lower() in lowered:
            return "insufficient_materials"
    return "success"


def _extract_summary(text: str, max_len: int = 200) -> str:
    cleaned = _MD_LINK_RE.sub(r"\1", text)
    cleaned = _MD_BOLD_RE.sub(r"\1", cleaned)
    cleaned = _MD_HEADER_RE.sub("", cleaned)
    cleaned = _MD_QUOTE_RE.sub("", cleaned)
    cleaned = cleaned.strip()
    paragraphs = [p.strip() for p in cleaned.split("\n\n") if p.strip()]
    candidate = paragraphs[0] if paragraphs else cleaned
    if len(candidate) < 20 and len(paragraphs) >= 2:
        candidate = paragraphs[1]
    return candidate[:max_len]


def _event_task_ids(event: AstrMessageEvent) -> list[str]:
    seen: set[str] = set()
    task_ids: list[str] = []
    for key in _AUTO_COMPLETE_TASK_ID_KEYS:
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


def _result_plain_text(event: AstrMessageEvent) -> str:
    result = event.get_result()
    if result is None or not result.chain:
        return ""
    parts: list[str] = []
    for comp in result.chain:
        if isinstance(comp, Plain):
            parts.append(comp.text or "")
    return "\n".join(parts).strip()


def _allows_auto_complete(task) -> bool:
    payload = getattr(task, "payload", {}) or {}
    explicit = payload.get("auto_complete_on_response")
    if explicit is not None:
        return bool(explicit)
    return payload.get("source") in _AUTO_COMPLETE_SOURCE_DEFAULTS


@register(
    "harness_sensor_plugin",
    "dc_agent",
    "Harness sensor 硬化（W0 Phase 0.2 重装移植版）",
    "1.0.0",
)
class HarnessSensorPlugin(Star):
    def __init__(self, context: Context) -> None:
        super().__init__(context)

    @filter.on_llm_response()
    async def maybe_complete_harness_task(
        self,
        event: AstrMessageEvent,
        resp: LLMResponse,
    ) -> None:
        """LLM 响应后检测：错误 → fail_task；成功 → complete_task with quality."""
        if not resp or not (getattr(resp, "completion_text", "") or "").strip():
            return

        text = (resp.completion_text or "").strip()
        quality = _classify_response_quality(resp, text)
        await self._settle_active_tasks(
            event,
            text=text,
            quality=quality,
            source="harness_sensor_plugin",
            role=getattr(resp, "role", None),
        )

    @filter.on_decorating_result(priority=20)
    async def maybe_complete_plugin_result(self, event: AstrMessageEvent) -> None:
        """Mark tasks for direct plugin replies that bypass the LLM response hook."""
        result = event.get_result()
        if result is None or result.is_model_result():
            return
        text = _result_plain_text(event)
        if not text:
            return
        await self._settle_active_tasks(
            event,
            text=text,
            quality=_classify_response_quality(None, text),
            source="harness_sensor_plugin:decorating_result",
            role=None,
            allowed_statuses={"pending", "in_progress", "review_required"},
        )

    async def _settle_active_tasks(
        self,
        event: AstrMessageEvent,
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

        tasks = await self._load_target_tasks(
            event,
            harness_engine,
            allowed_statuses=allowed_statuses,
        )
        if not tasks:
            return

        if quality == "error":
            for task in tasks:
                try:
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
                    await harness_engine.set_status(
                        task.task_id,
                        "blocked",
                        event_payload={
                            "reason": "insufficient_source_materials",
                            "response_preview": text[:500],
                            "source": source,
                        },
                    )
                    inbox_store = getattr(self.context, "ai_inbox_store", None)
                    if inbox_store is not None:
                        try:
                            item = await inbox_store.find_by_task_id(task.task_id)
                            if item is not None:
                                await inbox_store.update_item(
                                    item.item_id,
                                    status="waiting_materials",
                                    event_type="task_waiting_materials",
                                    event_payload={"source": source},
                                )
                        except Exception as exc:  # noqa: BLE001
                            logger.debug(
                                "[harness_sensor] inbox waiting-materials update skipped: %s",
                                exc,
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

        summary = _extract_summary(text)
        for task in tasks:
            try:
                await harness_engine.complete_task(
                    task.task_id,
                    result={
                        "summary": summary,
                        "response_preview": text[:500],
                        "source": source,
                        "quality": quality,
                        "role": role,
                    },
                )
                inbox_store = getattr(self.context, "ai_inbox_store", None)
                if inbox_store is not None:
                    try:
                        item = await inbox_store.find_by_task_id(task.task_id)
                        if item is not None:
                            await inbox_store.update_item(
                                item.item_id,
                                status="delivered",
                                event_type="task_response_delivered",
                                event_payload={"source": source},
                            )
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("[harness_sensor] inbox update skipped: %s", exc)
                logger.debug(
                    "[harness_sensor] Task %s completed (quality=%s, %d-char summary)",
                    task.task_id[:8],
                    quality,
                    len(summary),
                )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "[harness_sensor] complete_task %s failed",
                    task.task_id,
                    exc_info=True,
                )

    async def _load_target_tasks(
        self,
        event: AstrMessageEvent,
        harness_engine,
        *,
        allowed_statuses: set[str] | None = None,
    ):
        store = getattr(harness_engine, "store", None)
        if store is None:
            return []

        def _is_eligible(task) -> bool:
            return (
                task is not None
                and task.status not in _HARNESS_TERMINAL_STATUSES
                and _allows_auto_complete(task)
                and (allowed_statuses is None or task.status in allowed_statuses)
            )

        event_task_ids = _event_task_ids(event)
        if not event_task_ids:
            return []
        tasks = []
        for task_id in event_task_ids:
            try:
                task = await store.get_task(task_id)
            except Exception:  # noqa: BLE001
                task = None
            if _is_eligible(task):
                tasks.append(task)
        return tasks
