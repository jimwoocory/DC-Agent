"""AstrBot adapter for Harness state injection."""

from __future__ import annotations

from dc_engines.harness.runtime_hooks import (
    HarnessStateInjectionRuntime,
    format_task_line,
    safe_extra,
    should_inject_active_tasks,
)

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.provider.entities import ProviderRequest


@register(
    "harness_state_injector",
    "dc_agent",
    "Harness 任务状态硬约束注入（防止 LLM 假装『已分析』『已完成』）",
    "1.0.0",
)
class HarnessStateInjectorPlugin(Star):
    def __init__(self, context: Context) -> None:
        super().__init__(context)
        self._runtime = HarnessStateInjectionRuntime(context)

    @filter.on_llm_request(priority=35)
    async def inject_active_tasks(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> None:
        self._inject_truth_source_context(event, req)
        await self._runtime.inject_active_tasks(event, req)

    _format_task_line = staticmethod(format_task_line)
    _safe_extra = staticmethod(safe_extra)
    _should_inject_active_tasks = staticmethod(should_inject_active_tasks)

    @staticmethod
    def _inject_truth_source_context(
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> None:
        get_extra = getattr(event, "get_extra", None)
        if not callable(get_extra):
            return
        block = str(get_extra("dc_truth_source_context") or "").strip()
        if not block or "<dc_truth_source" not in block:
            return
        existing = req.system_prompt or ""
        if block in existing:
            return
        req.system_prompt = f"{existing.rstrip()}\n\n{block}\n" if existing else block
