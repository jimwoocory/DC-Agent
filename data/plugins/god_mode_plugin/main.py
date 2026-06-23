from __future__ import annotations

import json
from typing import Any

from dc_engines.card_runtime import send_card_via_runtime
from dc_engines.feishu_card_streamer import (
    ensure_streamers_on_context,
    extract_chat_info_from_event,
)
from dc_engines.god_mode import (
    CARD_SOURCE,
    DEFAULT_AUDIT_DB_PATH,
    GodModeAction,
    GodModeConfig,
    GodModeEngine,
    GodModeRequest,
    GodModeRun,
    build_god_mode_approval_card,
    build_god_mode_execution_card,
    parse_god_command,
)
from dc_engines.harness import HarnessTaskCreateRequest

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.star import Context, Star, register


@register(
    "god_mode_plugin",
    "dc_agent",
    "Feishu /god 管理员审批型超级工具入口",
    "0.1.0",
)
class GodModePlugin(Star):
    def __init__(self, context: Context) -> None:
        super().__init__(context)
        cfg = self._config()
        self.god_config = GodModeConfig.from_dict(cfg)
        db_path = self.god_config.audit_db_path or DEFAULT_AUDIT_DB_PATH
        self.god_engine = GodModeEngine(db_path)

    def _config(self) -> dict[str, Any]:
        getter = getattr(self.context, "get_config", None)
        if callable(getter):
            cfg = getter() or {}
            return cfg if isinstance(cfg, dict) else {}
        return {}

    def _sender_id(self, event: AstrMessageEvent) -> str:
        try:
            return str(event.get_sender_id() or "")
        except Exception:  # noqa: BLE001
            return ""

    def _platform_id(self, event: AstrMessageEvent) -> str:
        try:
            return str(event.get_platform_id() or "")
        except Exception:  # noqa: BLE001
            return ""

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        sender = self._sender_id(event)
        if sender and sender in set(self.god_config.owners):
            return True
        checker = getattr(event, "is_admin", None)
        if callable(checker):
            try:
                if bool(checker()):
                    return True
            except Exception:  # noqa: BLE001
                pass
        cfg = self._config()
        raw_admins = cfg.get("admins_id") or cfg.get("admin_ids") or []
        if isinstance(raw_admins, list | tuple | set):
            return sender in {str(item).strip() for item in raw_admins}
        return False

    def _is_trusted_card_action(self, event: AstrMessageEvent) -> bool:
        msg = getattr(event, "message_obj", None)
        return (
            getattr(event, "is_card_action", False) is True
            or getattr(msg, "is_card_action", False) is True
        )

    def _reply(self, event: AstrMessageEvent, text: str) -> None:
        event.set_result(MessageEventResult().message(text).use_t2i(False).stop_event())

    def _request_from_event(self, event: AstrMessageEvent, text: str) -> GodModeRequest:
        chat_id, _receive_id_type = self._safe_chat_info(event)
        sender = self._sender_id(event)
        return GodModeRequest(
            text=text,
            actor=sender,
            platform_id=self._platform_id(event),
            session_id=str(getattr(event, "unified_msg_origin", "") or ""),
            chat_id=chat_id,
            open_id=str(getattr(event, "open_id", "") or ""),
            message_id=str(getattr(event, "message_id", "") or ""),
            sender_id=sender,
            feishu_ingress_audit_id=str(
                self._safe_extra(event, "feishu_ingress_audit_id") or ""
            ),
            actor_is_admin=self._is_admin(event),
        )

    @filter.command("god", desc="/god <任务>|status|cancel")
    async def god_command(self, event: AstrMessageEvent) -> None:
        text = event.message_str or ""
        command = parse_god_command(text)
        if command.kind == "status":
            self._handle_status(event, command.run_id)
            return
        if command.kind == "cancel":
            self._handle_cancel(event, command.run_id)
            return

        request = self._request_from_event(event, text)
        run = self.god_engine.plan(
            request,
            self.god_config,
            executors=self._executors(),
        )
        if run.status == "failed" and not run.actions:
            logger.warning(
                "[god_mode] request rejected sender=%s platform=%s",
                self._sender_id(event),
                self._platform_id(event),
            )
            self._reply(event, run.summary)
            return
        if run.approval_required:
            if await self._send_approval_card(event, run):
                event.stop_event()
                return
            self._reply(event, _format_run(run))
            return
        self._reply(event, _format_run(run))

    @filter.regex(r"^__card_action__:")
    async def handle_card_action(self, event: AstrMessageEvent) -> None:
        payload = self._parse_card_action(event.message_str or "")
        value = payload.get("value") if isinstance(payload, dict) else {}
        value = value if isinstance(value, dict) else {}
        if value.get("source") != CARD_SOURCE:
            return

        if not self._is_trusted_card_action(event):
            logger.warning(
                "[god_mode] swallowed untrusted card action sender=%s",
                self._sender_id(event),
            )
            event.stop_event()
            return

        if not self._is_admin(event):
            logger.warning(
                "[god_mode] rejected non-admin approval sender=%s",
                self._sender_id(event),
            )
            self._reply(
                event, "抱歉，只有 God Mode owners 或管理员可以审批 /god 动作。"
            )
            return

        run_id = str(value.get("run_id") or "")
        action_id = str(value.get("action_id") or "")
        decision = str(value.get("decision") or "")
        if decision not in {"approve", "reject"} or not run_id or not action_id:
            self._reply(event, "审批参数缺失，已拒绝处理。")
            return

        try:
            run = await self.god_engine.apply_decision_async(
                run_id,
                action_id,
                decision,
                actor=self._sender_id(event),
                executors=self._executors(event),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[god_mode] approval failed run=%s action=%s: %s",
                run_id,
                action_id,
                exc,
            )
            self._reply(event, f"审批处理失败：{exc}")
            return

        self._reply(event, _format_run(run))

    def _handle_status(self, event: AstrMessageEvent, run_id: str) -> None:
        run = (
            self.god_engine.get_run(run_id) if run_id else self.god_engine.latest_run()
        )
        if run is None:
            self._reply(event, "未找到 /god run。")
            return
        self._reply(event, _format_run(run))

    def _handle_cancel(self, event: AstrMessageEvent, run_id: str) -> None:
        if not self._is_admin(event):
            self._reply(event, "抱歉，只有 God Mode owners 或管理员可以取消 /god run。")
            return
        if not run_id:
            self._reply(event, "用法：/god cancel <run_id>")
            return
        try:
            run = self.god_engine.cancel_run(run_id, actor=self._sender_id(event))
        except Exception as exc:  # noqa: BLE001
            self._reply(event, f"取消失败：{exc}")
            return
        self._reply(event, _format_run(run))

    async def _send_approval_card(
        self,
        event: AstrMessageEvent,
        run: GodModeRun,
    ) -> bool:
        streamer = ensure_streamers_on_context(self.context).get(
            event.get_platform_id() or ""
        )
        chat_id, receive_id_type = self._safe_chat_info(event)
        if streamer is None or not chat_id:
            return False
        stream = await send_card_via_runtime(
            streamer,
            card_type="god_mode_approval",
            chat_id=chat_id,
            receive_id_type=receive_id_type,
            card=build_god_mode_approval_card(run),
            platform_id=event.get_platform_id() or "",
            event="start",
            detail="god mode approval card",
        )
        return stream is not None

    def _executors(self, event: AstrMessageEvent | None = None) -> dict[str, Any]:
        return {
            "dc_agent_check_task_status": self._execute_status_action,
            "dc_agent_route_message": self._execute_route_action,
            "dc_agent_send_feishu_card": (
                lambda action: self._execute_send_feishu_card_action(event, action)
            ),
            "dc_agent_start_workflow": self._execute_start_workflow_action,
        }

    def _execute_status_action(self, action: GodModeAction) -> str:
        store = getattr(self.context, "harness_store", None)
        engine = getattr(self.context, "harness_engine", None)
        if store is None and engine is None:
            return (
                "Harness 状态查询完成：当前运行时未暴露 harness_store/harness_engine。"
            )
        return "Harness 状态查询完成：harness_runtime_plugin 已加载。"

    def _execute_route_action(self, action: GodModeAction) -> str:
        task = str(action.input.get("task") or "").strip()
        if not task:
            return "路由完成：空任务。"
        return "路由完成：已保持在 /god 工具入口，未进入普通 LLM 主链路。"

    async def _execute_send_feishu_card_action(
        self,
        event: AstrMessageEvent | None,
        action: GodModeAction,
    ) -> str:
        if event is None:
            return "飞书卡片动作已审批，但缺少当前飞书事件上下文。"
        streamer = ensure_streamers_on_context(self.context).get(
            event.get_platform_id() or ""
        )
        chat_id, receive_id_type = self._safe_chat_info(event)
        if streamer is None or not chat_id:
            return "飞书卡片动作已审批，但当前平台未配置 FeishuCardStreamer。"
        stream = await send_card_via_runtime(
            streamer,
            card_type="god_mode_execution",
            chat_id=chat_id,
            receive_id_type=receive_id_type,
            card=build_god_mode_execution_card(action),
            platform_id=event.get_platform_id() or "",
            event="start",
            detail="god mode approved execution card",
        )
        if stream is None:
            return "飞书卡片动作已审批，但运行时发送失败，已保留文本回退。"
        return f"飞书执行卡已发送：message_id={stream.message_id}"

    async def _execute_start_workflow_action(self, action: GodModeAction) -> str:
        engine = getattr(self.context, "harness_engine", None)
        if engine is None:
            return "工作流动作已审批，但 harness_engine 未初始化。"
        task_text = str(action.input.get("task") or "God Mode workflow").strip()
        task = await engine.create_task(
            HarnessTaskCreateRequest(
                title=task_text[:80],
                conversation_id=str(
                    action.input.get("session_id")
                    or action.input.get("chat_id")
                    or "god_mode"
                ),
                platform_id=str(action.input.get("platform_id") or "lark"),
                session_id=str(action.input.get("session_id") or ""),
                domain="god_mode",
                payload={
                    "source": "god_mode",
                    "tool_name": action.tool_name,
                    "capability": action.capability,
                    "message_text": task_text,
                    "chat_id": action.input.get("chat_id") or "",
                    "open_id": action.input.get("open_id") or "",
                    "message_id": action.input.get("message_id") or "",
                    "feishu_ingress_audit_id": action.input.get(
                        "feishu_ingress_audit_id"
                    )
                    or "",
                    "auto_complete_on_response": False,
                },
            )
        )
        return f"Harness 任务已创建：task_id={task.task_id}"

    def _safe_chat_info(self, event: AstrMessageEvent) -> tuple[str, str]:
        try:
            chat_id, receive_id_type = extract_chat_info_from_event(event)
        except Exception:  # noqa: BLE001
            return "", "chat_id"
        return str(chat_id or ""), str(receive_id_type or "chat_id")

    def _safe_extra(self, event: AstrMessageEvent, key: str) -> Any:
        getter = getattr(event, "get_extra", None)
        if callable(getter):
            try:
                return getter(key, default="")
            except TypeError:
                return getter(key)
            except Exception:  # noqa: BLE001
                return ""
        return getattr(event, key, "")

    @staticmethod
    def _parse_card_action(text: str) -> dict[str, Any]:
        if not text.startswith("__card_action__:"):
            return {}
        try:
            payload = json.loads(text[len("__card_action__:") :])
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}


def _format_run(run: GodModeRun) -> str:
    lines = [
        "God Mode",
        f"- run_id: {run.run_id}",
        f"- status: {run.status}",
        f"- summary: {run.summary}",
        f"- approval_required: {str(run.approval_required).lower()}",
        f"- audit_id: {run.audit_id}",
    ]
    if run.actions:
        lines.append("- actions:")
        lines.extend(
            (
                f"  - {action.action_id} · {action.tool_name} · "
                f"{action.status} · {action.description}"
                + (f" · {action.result_summary}" if action.result_summary else "")
            )
            for action in run.actions
        )
    return "\n".join(lines)
