"""本地 Agent 模式的 LLM 调用 Stage"""

import asyncio
import base64
from collections.abc import AsyncGenerator
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from astrbot.core import db_helper, logger
from astrbot.core.agent.message import (
    CheckpointData,
    CheckpointMessageSegment,
    Message,
    dump_messages_with_checkpoints,
)
from astrbot.core.agent.response import AgentStats
from astrbot.core.astr_main_agent import (
    LLM_ERROR_MESSAGE_EXTRA_KEY,
    MainAgentBuildConfig,
    MainAgentBuildResult,
    build_main_agent,
)
from astrbot.core.message.components import File, Image, Record, Reply, Video
from astrbot.core.message.message_event_result import (
    MessageChain,
    MessageEventResult,
    ResultContentType,
)
from astrbot.core.persona_error_reply import (
    extract_persona_custom_error_message_from_event,
)
from astrbot.core.pipeline.stage import Stage
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.provider.entities import (
    LLMResponse,
    ProviderRequest,
)
from astrbot.core.runtime_context.watermarks import (
    MemoryWatermarkAction,
    evaluate_medium_term_watermark,
    long_term_archive_action,
)
from astrbot.core.star.star_handler import EventType
from astrbot.core.utils.astrbot_path import get_astrbot_root
from astrbot.core.utils.metrics import Metric
from astrbot.core.utils.session_lock import session_lock_manager

from .....astr_agent_run_util import AgentRunner, run_agent, run_live_agent
from ....context import PipelineContext, call_event_hook
from ...follow_up import (
    FollowUpCapture,
    finalize_follow_up_capture,
    prepare_follow_up_capture,
    register_active_runner,
    try_capture_follow_up,
    unregister_active_runner,
)


class InternalAgentSubStage(Stage):
    async def initialize(self, ctx: PipelineContext) -> None:
        self.ctx = ctx
        conf = ctx.astrbot_config
        settings = conf["provider_settings"]
        self.streaming_response: bool = settings["streaming_response"]
        self.unsupported_streaming_strategy: str = settings[
            "unsupported_streaming_strategy"
        ]
        self.max_step: int = settings.get("max_agent_step", 30)
        self.tool_call_timeout: int = settings.get("tool_call_timeout", 60)
        self.tool_schema_mode: str = settings.get("tool_schema_mode", "full")
        if self.tool_schema_mode not in ("skills_like", "full"):
            logger.warning(
                "Unsupported tool_schema_mode: %s, fallback to skills_like",
                self.tool_schema_mode,
            )
            self.tool_schema_mode = "full"
        if isinstance(self.max_step, bool):  # workaround: #2622
            self.max_step = 30
        self.show_tool_use: bool = settings.get("show_tool_use_status", True)
        self.show_tool_call_result: bool = settings.get("show_tool_call_result", False)
        self.buffer_intermediate_messages: bool = settings.get(
            "buffer_intermediate_messages",
            False,
        )
        self.show_reasoning = settings.get("display_reasoning_text", False)
        self.sanitize_context_by_modalities: bool = settings.get(
            "sanitize_context_by_modalities",
            False,
        )
        self.kb_agentic_mode: bool = conf.get("kb_agentic_mode", False)

        file_extract_conf: dict = settings.get("file_extract", {})
        self.file_extract_enabled: bool = file_extract_conf.get("enable", False)
        self.file_extract_prov: str = file_extract_conf.get("provider", "moonshotai")
        self.file_extract_msh_api_key: str = file_extract_conf.get(
            "moonshotai_api_key", ""
        )

        # 上下文管理相关
        self.context_limit_reached_strategy: str = settings.get(
            "context_limit_reached_strategy", "truncate_by_turns"
        )
        self.llm_compress_instruction: str = settings.get(
            "llm_compress_instruction", ""
        )
        self.llm_compress_keep_recent_ratio: float = settings.get(
            "llm_compress_keep_recent_ratio", 0.15
        )
        self.llm_compress_provider_id: str = settings.get(
            "llm_compress_provider_id", ""
        )
        self.max_context_length = settings["max_context_length"]  # int
        self.dequeue_context_length: int = min(
            max(1, settings["dequeue_context_length"]),
            self.max_context_length - 1,
        )
        if self.dequeue_context_length <= 0:
            self.dequeue_context_length = 1
        self.fallback_max_context_tokens: int = settings.get(
            "fallback_max_context_tokens", 128000
        )

        self.llm_safety_mode = settings.get("llm_safety_mode", True)
        self.safety_mode_strategy = settings.get(
            "safety_mode_strategy", "system_prompt"
        )

        self.computer_use_runtime = settings.get("computer_use_runtime")
        self.sandbox_cfg = settings.get("sandbox", {})

        # Proactive capability configuration
        proactive_cfg = settings.get("proactive_capability", {})
        self.add_cron_tools = proactive_cfg.get("add_cron_tools", True)

        self.conv_manager = ctx.plugin_manager.context.conversation_manager

        self.main_agent_cfg = MainAgentBuildConfig(
            tool_call_timeout=self.tool_call_timeout,
            tool_schema_mode=self.tool_schema_mode,
            sanitize_context_by_modalities=self.sanitize_context_by_modalities,
            kb_agentic_mode=self.kb_agentic_mode,
            file_extract_enabled=self.file_extract_enabled,
            file_extract_prov=self.file_extract_prov,
            file_extract_msh_api_key=self.file_extract_msh_api_key,
            context_limit_reached_strategy=self.context_limit_reached_strategy,
            llm_compress_instruction=self.llm_compress_instruction,
            llm_compress_keep_recent_ratio=self.llm_compress_keep_recent_ratio,
            llm_compress_provider_id=self.llm_compress_provider_id,
            max_context_length=self.max_context_length,
            dequeue_context_length=self.dequeue_context_length,
            fallback_max_context_tokens=self.fallback_max_context_tokens,
            llm_safety_mode=self.llm_safety_mode,
            safety_mode_strategy=self.safety_mode_strategy,
            computer_use_runtime=self.computer_use_runtime,
            sandbox_cfg=self.sandbox_cfg,
            add_cron_tools=self.add_cron_tools,
            provider_settings=settings,
            subagent_orchestrator=conf.get("subagent_orchestrator", {}),
            timezone=self.ctx.plugin_manager.context.get_config().get("timezone"),
            max_quoted_fallback_images=settings.get("max_quoted_fallback_images", 20),
        )

    async def _send_llm_error_message(
        self, event: AstrMessageEvent, message: object
    ) -> None:
        await event.send(MessageChain().message(str(message)))

    async def process(
        self, event: AstrMessageEvent, provider_wake_prefix: str
    ) -> AsyncGenerator[None, None]:
        follow_up_capture: FollowUpCapture | None = None
        follow_up_consumed_marked = False
        follow_up_activated = False
        typing_requested = False
        try:
            streaming_response = self.streaming_response
            if (enable_streaming := event.get_extra("enable_streaming")) is not None:
                streaming_response = bool(enable_streaming)
            if _is_router_web_search_required(event):
                streaming_response = False

            has_provider_request = event.get_extra("provider_request") is not None
            has_valid_message = bool(event.message_str and event.message_str.strip())
            has_media_content = any(
                isinstance(comp, (Image, File, Record, Video))
                for comp in event.message_obj.message
            )
            has_reply = any(
                isinstance(comp, Reply) for comp in event.message_obj.message
            )

            if (
                not has_provider_request
                and not has_valid_message
                and not has_media_content
                and not has_reply
            ):
                logger.debug("skip llm request: empty message and no provider_request")
                return

            logger.debug("ready to request llm provider")
            follow_up_capture = try_capture_follow_up(event)
            if follow_up_capture:
                (
                    follow_up_consumed_marked,
                    follow_up_activated,
                ) = await prepare_follow_up_capture(follow_up_capture)
                if follow_up_consumed_marked:
                    logger.info(
                        "Follow-up ticket already consumed, stopping processing. umo=%s, seq=%s",
                        event.unified_msg_origin,
                        follow_up_capture.ticket.seq,
                    )
                    return

            try:
                typing_requested = True
                await event.send_typing()
            except Exception:
                logger.warning("send_typing failed", exc_info=True)
            if await call_event_hook(event, EventType.OnWaitingLLMRequestEvent):
                return

            async with session_lock_manager.acquire_lock(event.unified_msg_origin):
                logger.debug("acquired session lock for llm request")
                agent_runner: AgentRunner | None = None
                runner_registered = False
                try:
                    build_cfg = replace(
                        self.main_agent_cfg,
                        provider_wake_prefix=provider_wake_prefix,
                        streaming_response=streaming_response,
                    )

                    build_result: MainAgentBuildResult | None = await build_main_agent(
                        event=event,
                        plugin_context=self.ctx.plugin_manager.context,
                        config=build_cfg,
                        apply_reset=False,
                    )

                    if build_result is None:
                        if llm_error_message := event.get_extra(
                            LLM_ERROR_MESSAGE_EXTRA_KEY
                        ):
                            await self._send_llm_error_message(
                                event,
                                llm_error_message,
                            )
                        return

                    agent_runner = build_result.agent_runner
                    req = build_result.provider_request
                    provider = build_result.provider
                    reset_coro = build_result.reset_coro

                    api_base = provider.provider_config.get("api_base", "")
                    for host in decoded_blocked:
                        if host in api_base:
                            error_message = (
                                f"LLM 请求失败：Provider API base `{api_base}` "
                                "因安全原因被拦截，请更换可用的 AI 提供商。"
                            )
                            logger.error(error_message)
                            await self._send_llm_error_message(event, error_message)
                            return

                    stream_to_general = (
                        self.unsupported_streaming_strategy == "turn_off"
                        and not event.platform_meta.support_streaming_message
                    )

                    if await call_event_hook(event, EventType.OnLLMRequestEvent, req):
                        if reset_coro:
                            reset_coro.close()
                        return

                    # apply reset
                    if reset_coro:
                        await reset_coro

                    register_active_runner(event.unified_msg_origin, agent_runner)
                    runner_registered = True
                    action_type = event.get_extra("action_type")

                    event.trace.record(
                        "astr_agent_prepare",
                        system_prompt=req.system_prompt,
                        tools=req.func_tool.names() if req.func_tool else [],
                        stream=streaming_response,
                        chat_provider={
                            "id": provider.provider_config.get("id", ""),
                            "model": provider.get_model(),
                        },
                    )

                    # 检测 Live Mode
                    if action_type == "live":
                        # Live Mode: 使用 run_live_agent
                        logger.info("[Internal Agent] 检测到 Live Mode，启用 TTS 处理")

                        # 获取 TTS Provider
                        tts_provider = (
                            self.ctx.plugin_manager.context.get_using_tts_provider(
                                event.unified_msg_origin
                            )
                        )

                        if not tts_provider:
                            logger.warning(
                                "[Live Mode] TTS Provider 未配置，将使用普通流式模式"
                            )

                        # 使用 run_live_agent，总是使用流式响应
                        event.set_result(
                            MessageEventResult()
                            .set_result_content_type(ResultContentType.STREAMING_RESULT)
                            .set_async_stream(
                                run_live_agent(
                                    agent_runner,
                                    tts_provider,
                                    self.max_step,
                                    self.show_tool_use,
                                    self.show_tool_call_result,
                                    show_reasoning=self.show_reasoning,
                                    buffer_intermediate_messages=self.buffer_intermediate_messages,
                                ),
                            ),
                        )
                        yield

                        # 保存历史记录
                        if agent_runner.done() and (
                            not event.is_stopped() or agent_runner.was_aborted()
                        ):
                            await self._save_to_history(
                                event,
                                req,
                                agent_runner.get_final_llm_resp(),
                                agent_runner.run_context.messages,
                                agent_runner.stats,
                                user_aborted=agent_runner.was_aborted(),
                            )

                    elif streaming_response and not stream_to_general:
                        # 流式响应
                        event.set_result(
                            MessageEventResult()
                            .set_result_content_type(ResultContentType.STREAMING_RESULT)
                            .set_async_stream(
                                run_agent(
                                    agent_runner,
                                    self.max_step,
                                    self.show_tool_use,
                                    self.show_tool_call_result,
                                    show_reasoning=self.show_reasoning,
                                    buffer_intermediate_messages=self.buffer_intermediate_messages,
                                ),
                            ),
                        )
                        yield
                        if agent_runner.done():
                            if final_llm_resp := agent_runner.get_final_llm_resp():
                                if final_llm_resp.completion_text:
                                    chain = (
                                        MessageChain()
                                        .message(final_llm_resp.completion_text)
                                        .chain
                                    )
                                elif final_llm_resp.result_chain:
                                    chain = final_llm_resp.result_chain.chain
                                else:
                                    chain = MessageChain().chain
                                event.set_result(
                                    MessageEventResult(
                                        chain=chain,
                                        result_content_type=ResultContentType.STREAMING_FINISH,
                                    ),
                                )
                    else:
                        async for _ in run_agent(
                            agent_runner,
                            self.max_step,
                            self.show_tool_use,
                            self.show_tool_call_result,
                            stream_to_general,
                            show_reasoning=self.show_reasoning,
                            buffer_intermediate_messages=self.buffer_intermediate_messages,
                        ):
                            yield

                    final_resp = agent_runner.get_final_llm_resp()

                    event.trace.record(
                        "astr_agent_complete",
                        stats=agent_runner.stats.to_dict(),
                        resp=final_resp.completion_text if final_resp else None,
                    )

                    asyncio.create_task(
                        _record_internal_agent_stats(
                            event,
                            req,
                            agent_runner,
                            final_resp,
                        )
                    )

                    # 检查事件是否被停止，如果被停止则不保存历史记录
                    if not event.is_stopped() or agent_runner.was_aborted():
                        await self._save_to_history(
                            event,
                            req,
                            final_resp,
                            agent_runner.run_context.messages,
                            agent_runner.stats,
                            user_aborted=agent_runner.was_aborted(),
                        )

                    asyncio.create_task(
                        Metric.upload(
                            llm_tick=1,
                            model_name=agent_runner.provider.get_model(),
                            provider_type=agent_runner.provider.meta().type,
                        ),
                    )
                finally:
                    if runner_registered and agent_runner is not None:
                        unregister_active_runner(event.unified_msg_origin, agent_runner)

        except Exception as e:
            logger.error(f"Error occurred while processing agent: {e}")
            custom_error_message = extract_persona_custom_error_message_from_event(
                event
            )
            error_text = custom_error_message or (
                f"Error occurred while processing agent request: {e}"
            )
            await event.send(MessageChain().message(error_text))
        finally:
            if typing_requested:
                try:
                    await event.stop_typing()
                except Exception:
                    logger.warning("stop_typing failed", exc_info=True)
            if follow_up_capture:
                await finalize_follow_up_capture(
                    follow_up_capture,
                    activated=follow_up_activated,
                    consumed_marked=follow_up_consumed_marked,
                )

    async def _save_to_history(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
        llm_response: LLMResponse | None,
        all_messages: list[Message],
        runner_stats: AgentStats | None,
        user_aborted: bool = False,
    ) -> None:
        if not req or not req.conversation:
            return

        if not llm_response and not user_aborted:
            return

        if llm_response and llm_response.role != "assistant":
            if not user_aborted:
                return
            llm_response = LLMResponse(
                role="assistant",
                completion_text=llm_response.completion_text or "",
            )
        elif llm_response is None:
            llm_response = LLMResponse(role="assistant", completion_text="")

        if (
            not llm_response.completion_text
            and not req.tool_calls_result
            and not user_aborted
        ):
            logger.debug("LLM 响应为空，不保存记录。")
            return

        messages_to_save: list[Message] = []
        skipped_initial_system = False
        for message in all_messages:
            if message.role == "system" and not skipped_initial_system:
                skipped_initial_system = True
                continue
            if message.role in ["assistant", "user"] and message._no_save:
                continue
            messages_to_save.append(message)

        checkpoint_id = event.get_extra("llm_checkpoint_id")
        message_to_save = dump_messages_with_checkpoints(messages_to_save)
        if isinstance(checkpoint_id, str) and checkpoint_id:
            message_to_save.append(
                CheckpointMessageSegment(
                    content=CheckpointData(id=checkpoint_id),
                ).model_dump()
            )

        # if user_aborted:
        #     message_to_save.append(
        #         Message(
        #             role="assistant",
        #             content="[User aborted this request. Partial output before abort was preserved.]",
        #         ).model_dump()
        #     )

        token_usage = None
        if runner_stats:
            # token_usage = runner_stats.token_usage.total
            token_usage = llm_response.usage.total if llm_response.usage else None

        await self.conv_manager.update_conversation(
            event.unified_msg_origin,
            req.conversation.cid,
            history=message_to_save,
            token_usage=token_usage,
        )
        await self._archive_lark_chat_turn(
            event,
            req,
            llm_response,
            token_usage=token_usage,
        )

    async def _archive_lark_chat_turn(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
        llm_response: LLMResponse,
        *,
        token_usage: int | None,
    ) -> None:
        if event.get_platform_name() != "lark":
            return

        user_text = (req.prompt or event.get_message_str() or "").strip()
        assistant_text = (llm_response.completion_text or "").strip()
        if not user_text and not assistant_text:
            return

        try:
            from dc_engines.chat_archive import (
                LarkChatArchiveRecord,
                append_lark_chat_record,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Lark chat archive unavailable: %s", exc)
            return

        try:
            open_id = event.get_sender_id() or ""
            employee_name, department = await self._resolve_lark_employee_profile(
                open_id=open_id,
                fallback_name=event.get_sender_name(),
            )
            await self._evaluate_lark_medium_memory_watermark(open_id, event)

            root = self._lark_chat_archive_root()
            timestamp = _event_timestamp(event)
            message_id = _lark_message_id(event)
            archive_action = long_term_archive_action()
            common = {
                "platform_id": event.get_platform_id(),
                "conversation_id": req.conversation.cid if req.conversation else "",
                "message_id": message_id,
                "feishu_open_id": open_id,
                "employee_name": employee_name,
                "department": department,
                "metadata": {
                    "unified_msg_origin": event.unified_msg_origin,
                    "long_term_action": archive_action.action.value,
                    "long_term_handoff": archive_action.metadata.get("handoff", ""),
                },
            }
            if user_text:
                await asyncio.to_thread(
                    append_lark_chat_record,
                    root,
                    LarkChatArchiveRecord(
                        timestamp=timestamp,
                        sender_type="user",
                        text=user_text,
                        **common,
                    ),
                )
            if assistant_text:
                await asyncio.to_thread(
                    append_lark_chat_record,
                    root,
                    LarkChatArchiveRecord(
                        timestamp=_now_timestamp(),
                        sender_type="assistant",
                        text=assistant_text,
                        total_tokens=token_usage,
                        **common,
                    ),
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Persist Lark chat archive failed: %s", exc, exc_info=True)

    async def _resolve_lark_employee_profile(
        self,
        *,
        open_id: str,
        fallback_name: str,
    ) -> tuple[str, str]:
        employee_name = fallback_name.strip() or "未知员工"
        department = "未知部门"
        store = getattr(self.ctx.plugin_manager.context, "employee_store", None)
        if not store or not open_id:
            return employee_name, department

        try:
            employee = await store.get_employee(open_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Load Lark employee profile failed: %s", exc)
            return employee_name, department

        if employee is None:
            return employee_name, department
        return (
            employee.display_name or employee_name,
            employee.department or department,
        )

    async def _evaluate_lark_medium_memory_watermark(
        self,
        open_id: str,
        event: AstrMessageEvent,
    ) -> None:
        store = getattr(self.ctx.plugin_manager.context, "employee_store", None)
        if not store or not open_id:
            return

        try:
            memories = await store.list_memories(open_id, limit=1000)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Evaluate Lark medium memory watermark failed: %s", exc)
            return

        token_estimate = sum(max(1, len(memory.content) // 3) for memory in memories)
        decision = evaluate_medium_term_watermark(
            item_count=len(memories),
            token_estimate=token_estimate,
        )
        event.set_extra(
            "runtime_context_medium_watermark",
            {
                "layer": decision.layer.value,
                "action": decision.action.value,
                "current": decision.current,
                "limit": decision.limit,
                "reason": decision.reason,
                "metadata": dict(decision.metadata),
            },
        )
        if decision.action == MemoryWatermarkAction.CLEANUP_AND_PROMOTE:
            logger.info(
                "Lark medium memory watermark reached for open_id=%s: %s",
                open_id,
                decision.reason,
            )

    def _lark_chat_archive_root(self) -> Path:
        archive_cfg = self.main_agent_cfg.provider_settings.get("lark_chat_archive", {})
        if not isinstance(archive_cfg, dict):
            archive_cfg = {}
        configured_root = archive_cfg.get("nas_root")
        if configured_root:
            return Path(str(configured_root)).expanduser()
        return Path(get_astrbot_root()) / "nas"


# we prevent astrbot from connecting to known malicious hosts
# these hosts are base64 encoded
BLOCKED = {"dGZid2h2d3IuY2xvdWQuc2VhbG9zLmlv", "a291cmljaGF0"}
decoded_blocked = [base64.b64decode(b).decode("utf-8") for b in BLOCKED]


def _now_timestamp() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _event_timestamp(event: AstrMessageEvent) -> str:
    created_at = getattr(event, "created_at", None)
    if isinstance(created_at, int | float) and created_at > 0:
        return datetime.fromtimestamp(created_at, timezone.utc).astimezone().isoformat()
    return _now_timestamp()


def _lark_message_id(event: AstrMessageEvent) -> str:
    raw_message = getattr(event.message_obj, "raw_message", None)
    if isinstance(raw_message, dict):
        for key in ("message_id", "open_message_id"):
            value = raw_message.get(key)
            if value:
                return str(value)
    else:
        for key in ("message_id", "open_message_id"):
            value = getattr(raw_message, key, None)
            if value:
                return str(value)

    extra = event.get_extra("message_id")
    return str(extra) if extra else ""


def _is_router_web_search_required(event: AstrMessageEvent) -> bool:
    raw_value = event.get_extra("dc_router_meta_search_required")
    if isinstance(raw_value, bool):
        return raw_value
    return str(raw_value or "").strip().lower() in {"1", "true", "yes", "on"}


async def _record_internal_agent_stats(
    event: AstrMessageEvent,
    req: ProviderRequest | None,
    agent_runner: AgentRunner | None,
    final_resp: LLMResponse | None,
) -> None:
    """Persist internal agent stats without affecting the user response flow."""
    if agent_runner is None:
        return

    provider = agent_runner.provider
    stats = agent_runner.stats
    if provider is None or stats is None:
        return

    try:
        provider_config = getattr(provider, "provider_config", {}) or {}
        conversation_id = (
            req.conversation.cid
            if req is not None and req.conversation is not None
            else None
        )

        if agent_runner.was_aborted():
            status = "aborted"
        elif final_resp is not None and final_resp.role == "err":
            status = "error"
        else:
            status = "completed"

        await db_helper.insert_provider_stat(
            umo=event.unified_msg_origin,
            conversation_id=conversation_id,
            provider_id=provider_config.get("id", "") or provider.meta().id,
            provider_model=provider.get_model(),
            status=status,
            stats=stats.to_dict(),
            agent_type="internal",
        )
    except Exception as e:
        logger.warning("Persist provider stats failed: %s", e, exc_info=True)
