"""AstrBot adapter for Harness task settlement hooks."""

from __future__ import annotations

from dc_engines.harness import runtime_hooks as _runtime_hooks
from dc_engines.harness.runtime_hooks import HarnessSensorRuntime

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse
from astrbot.api.star import Context, Star, register

_classify_response_quality = _runtime_hooks.classify_response_quality
_event_task_ids = _runtime_hooks.event_task_ids
_extract_summary = _runtime_hooks.extract_summary
_result_plain_text = _runtime_hooks.result_plain_text


@register(
    "harness_sensor_plugin",
    "dc_agent",
    "Harness sensor 硬化（W0 Phase 0.2 重装移植版）",
    "1.0.0",
)
class HarnessSensorPlugin(Star):
    def __init__(self, context: Context) -> None:
        super().__init__(context)
        self._runtime = HarnessSensorRuntime(context)

    def _runtime_adapter(self) -> HarnessSensorRuntime:
        runtime = getattr(self, "_runtime", None)
        if runtime is None:
            runtime = HarnessSensorRuntime(self.context)
            self._runtime = runtime
        return runtime

    @filter.on_llm_response()
    async def maybe_complete_harness_task(
        self,
        event: AstrMessageEvent,
        resp: LLMResponse,
    ) -> None:
        await self._runtime_adapter().settle_llm_response(event, resp)

    @filter.on_decorating_result(priority=20)
    async def maybe_complete_plugin_result(self, event: AstrMessageEvent) -> None:
        await self._runtime_adapter().settle_plugin_result(event)

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
        await self._runtime_adapter().settle_active_tasks(
            event,
            text=text,
            quality=quality,
            source=source,
            role=role,
            allowed_statuses=allowed_statuses,
        )

    async def _load_target_tasks(
        self,
        event: AstrMessageEvent,
        harness_engine,
        *,
        allowed_statuses: set[str] | None = None,
    ):
        return await self._runtime_adapter().load_target_tasks(
            event,
            harness_engine,
            allowed_statuses=allowed_statuses,
        )
