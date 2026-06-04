"""Hermes Escalation Star 插件（重装移植版）。

旧版逻辑：router_stage._check_hermes_escalation + _handle_hermes_escalation。
监听群消息，用 SatisfactionDetector 检测三层信号：
  1. 显式 "让 hermes 来" / "交给 hermes" / "hermes 处理" (confidence 0.98)
  2. 高置信度不满 (confidence 0.88) — "不够深入"/"深度分析"/"深挖一下"等
  3. 中置信度不满 (confidence 0.70) — 仅当会话已有活跃 task 时升级

命中即：
  - 找当前活跃 Harness task（没有就用 project_followup workflow 建一个）
  - mark_in_progress + note="dispatched_to_hermes"
  - POST 到 Hermes task_webhook_url（HMAC 签名）
  - 拦截事件并回复 "已安排 Hermes 深度处理"

依赖：
- ``dc_engines.harness.satisfaction.SatisfactionDetector`` （已迁移）
- ``dc_engines.harness.create_workflow_request`` （已迁移）
- ``self.context.harness_engine`` （hermes_bridge plugin initialize 时装入）
- ``hermes_bridge`` 配置 (task_webhook_url + secret)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os

import aiohttp
from dc_engines.card_runtime import send_card_via_runtime
from dc_engines.employee_directory import requester_meta_from_event
from dc_engines.feishu_card_streamer import (
    build_progress_card,
    ensure_streamers_on_context,
)
from dc_engines.harness import create_workflow_request
from dc_engines.harness.satisfaction import SatisfactionDetector

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.star import Context, Star, register


@register(
    "hermes_escalation_plugin",
    "dc_agent",
    "Hermes 升级派发（W0 重装移植版），不满意/显式请求 → 自动派发到 Hermes 深度执行",
    "1.0.0",
)
class HermesEscalationPlugin(Star):
    def __init__(self, context: Context) -> None:
        super().__init__(context)
        self._detector = SatisfactionDetector()

    def _bridge_cfg(self) -> dict:
        """读 hermes_bridge 配置，跟 hermes_bridge plugin 同源（避免 secret 分裂）。

        优先 data/config/hermes_bridge_config.json（plugin 实际用的），
        没有再回退 cmd_config.json 的 hermes_bridge 节。
        """
        # 同源：plugin config 文件
        try:
            from pathlib import Path

            cfg_path = Path(
                "/Users/dianchi/DC-Agent/data/config/hermes_bridge_config.json"
            )
            if cfg_path.exists():
                data = json.loads(cfg_path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data:
                    return data
        except Exception as exc:
            logger.debug(
                "[hermes_escalation] 读 hermes_bridge_config.json 失败: %s", exc
            )
        # 回退：cmd_config.json 的 hermes_bridge 节
        cfg = self.context.get_config() or {}
        return cfg.get("hermes_bridge", {}) if isinstance(cfg, dict) else {}

    def _sign(self, secret: str, payload_bytes: bytes) -> str:
        digest = hmac.new(
            secret.encode("utf-8"), payload_bytes, hashlib.sha256
        ).hexdigest()
        return f"sha256={digest}"

    def _require_secret(self, bridge: dict) -> str:
        raw_secret = str(bridge.get("secret") or "").strip()
        if raw_secret.startswith("${") and raw_secret.endswith("}"):
            raw_secret = os.environ.get(raw_secret[2:-1], "").strip()
        if not raw_secret:
            raise RuntimeError(
                "hermes bridge secret 未配置，请补 data/config/hermes_bridge_config.json"
            )
        return raw_secret

    async def _dispatch_to_hermes(
        self, task, intent_workflow_kind: str, event: AstrMessageEvent
    ) -> bool:
        bridge = self._bridge_cfg()
        url = bridge.get(
            "task_webhook_url", "http://localhost:8644/webhooks/astrbot_task"
        )
        secret = self._require_secret(bridge)

        cognitive_context: dict = {}
        if hasattr(task, "payload") and isinstance(task.payload, dict):
            cognitive_context = task.payload.get("cognitive_context") or {}

        payload = {
            "task_id": task.task_id,
            "workflow_kind": intent_workflow_kind,
            "brief": (event.message_str or "").strip(),
            "session_id": event.unified_msg_origin,
            "unified_msg_origin": event.unified_msg_origin,
            "platform_id": event.get_platform_id(),
            "sender_id": event.get_sender_id(),
            "trigger_message": event.message_str,
            "cognitive_context": cognitive_context,
        }
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "X-Webhook-Event": "harness_task",
            "X-Task-ID": task.task_id,
            "X-Hub-Signature-256": self._sign(secret, body),
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    data=body,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status in (200, 201, 202):
                        logger.info(
                            "[hermes_escalation] 派发成功 task=%s url=%s",
                            task.task_id[:8],
                            url,
                        )
                        return True
                    text = await resp.text()
                    logger.warning(
                        "[hermes_escalation] 派发失败 HTTP %s: %s",
                        resp.status,
                        text[:200],
                    )
                    return False
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[hermes_escalation] 派发异常 task=%s: %s", task.task_id[:8], exc
            )
            return False

    @filter.event_message_type(
        EventMessageType.GROUP_MESSAGE | EventMessageType.PRIVATE_MESSAGE
    )
    async def on_message(self, event: AstrMessageEvent):
        text = (event.message_str or "").strip()
        if not text:
            return

        # 群里必须 @DC-Agent（私聊无门槛）—— 跟 workflow_intent 一致
        is_group = "GroupMessage" in (event.unified_msg_origin or "")
        if is_group and not getattr(event, "is_at_or_wake_command", False):
            return

        # 满意度检测
        signal = self._detector.detect(text)
        if not signal.dissatisfied:
            return

        # 决定是否升级（沿用旧版三层逻辑）
        engine = getattr(self.context, "harness_engine", None)
        store = getattr(self.context, "harness_store", None)
        if engine is None or store is None:
            return  # Harness 没就绪 → 静默跳过

        should_escalate = False
        if signal.is_explicit_hermes_request or signal.confidence >= 0.88:
            should_escalate = True
        elif signal.confidence >= 0.65:
            # 中置信度：仅当已有活跃 task 才升级
            try:
                active = await store.list_tasks_for_session(
                    event.unified_msg_origin,
                    limit=5,
                    statuses=("pending", "in_progress", "blocked", "review_required"),
                )
                if active:
                    should_escalate = True
            except Exception:  # noqa: BLE001
                pass

        if not should_escalate:
            return

        # 找已有活跃 task；没有就建 project_followup
        task = None
        try:
            tasks = await store.list_tasks_for_session(
                event.unified_msg_origin,
                limit=5,
                statuses=("pending", "in_progress", "blocked", "review_required"),
            )
            task = tasks[0] if tasks else None
        except Exception as exc:  # noqa: BLE001
            logger.debug("[hermes_escalation] list_tasks 失败：%s", exc)

        if task is None:
            try:
                conv_id = (
                    await self.context.conversation_manager.get_curr_conversation_id(
                        event.unified_msg_origin
                    )
                )
                if not conv_id:
                    conv_id = await self.context.conversation_manager.new_conversation(
                        event.unified_msg_origin, event.get_platform_id()
                    )
                req = create_workflow_request(
                    workflow_kind="project_followup",
                    brief=text[:200],
                    conversation_id=conv_id,
                    platform_id=event.get_platform_id(),
                    session_id=event.unified_msg_origin,
                    source="satisfaction_escalation",
                    message_text=text,
                )
                req.payload.update(await requester_meta_from_event(self.context, event))
                req.payload["auto_complete_on_response"] = False
                task = await engine.create_task(req)
            except Exception:
                logger.warning("[hermes_escalation] 创建任务失败", exc_info=True)
                return

        if task is None:
            return

        try:
            case_engine = getattr(self.context, "case_engine", None)
            if case_engine is not None:
                case = await case_engine.get_current_case_for_session(
                    event.unified_msg_origin
                )
                if case is None:
                    ensure_case = getattr(self.context, "ai_inbox_ensure_case", None)
                    if ensure_case is not None:
                        case_id = await ensure_case(
                            event,
                            category="escalation",
                            text=text,
                            task_id=task.task_id,
                        )
                        if case_id:
                            case = await case_engine.store.get_case(case_id)
                if case is not None:
                    await case_engine.attach_task(case.case_id, task.task_id)
                    link_task = getattr(self.context, "ai_inbox_link_task", None)
                    if link_task is not None:
                        await link_task(
                            event,
                            task.task_id,
                            case_id=case.case_id,
                            source="hermes_escalation_plugin",
                        )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[hermes_escalation] case/inbox attach skipped: %s", exc)

        # mark in_progress + dispatch
        try:
            await engine.mark_in_progress(task.task_id, note="dispatched_to_hermes")
        except Exception as exc:  # noqa: BLE001
            logger.debug("[hermes_escalation] mark_in_progress 失败（不阻断）：%s", exc)

        workflow_kind = (
            task.payload.get("workflow_kind", "project_followup")
            if hasattr(task, "payload") and isinstance(task.payload, dict)
            else "project_followup"
        )

        dispatched = await self._dispatch_to_hermes(task, workflow_kind, event)

        # 派发失败 → 立刻标 task=failed，防止留僵尸 task 让 LLM 后续误判"还在跑"
        if not dispatched:
            try:
                await engine.fail_task(
                    task.task_id,
                    reason="dispatch_failed_to_hermes_gateway",
                )
                logger.warning(
                    "[hermes_escalation] task=%s 已标 failed（派发失败）",
                    task.task_id[:8],
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("[hermes_escalation] fail_task 也失败：%s", exc)

        # ─── 派发成功：发飞书进度卡片（解决「等待空荡荡」体验痛点）───
        if dispatched:
            await self._send_progress_card(event, task)

        # 回复并 stop_event —— 按派发结果如实告知
        if dispatched:
            # 卡片已发，文本 reply 简短即可
            reply = "⏳ Hermes 处理中（看下方卡片实时进度）"
        else:
            # 派发失败：不让 LLM 后续编造"已分析"，直接告知用户
            reply = (
                "⚠️ Hermes 派发暂时失败（看后台 hermes_escalation 日志定位）。"
                "我已经把任务记下来 task_id="
                f"{task.task_id[:8]}，"
                "你可以重试，或者直接把核心问题贴过来我用当前模型先帮你过一遍。"
            )

        event.set_result(
            MessageEventResult().message(reply).use_t2i(False).stop_event()
        )

        logger.info(
            "[hermes_escalation] 升级成功 task=%s session=%s reason=%s confidence=%.2f",
            task.task_id[:8],
            event.unified_msg_origin,
            signal.reason,
            signal.confidence,
        )

    # ─── 飞书进度卡片：解决"等待空荡荡"UX 痛点 ───

    async def _send_progress_card(self, event: AstrMessageEvent, task) -> None:
        """派发到 Hermes 后立刻发飞书进度卡片，stream_id 存到 context 共享 map。"""
        platform_id = event.get_platform_id() or ""
        streamers = ensure_streamers_on_context(self.context)
        streamer = streamers.get(platform_id)
        if streamer is None:
            logger.debug(
                "[hermes_escalation] 未找到 platform=%r 的 streamer，跳过卡片",
                platform_id,
            )
            return

        # 拿 chat_id（lark p2p 时 chat_id 是 oc_xxx，群聊也是 oc_xxx）
        # event.get_group_id() 群聊有，私聊 None；event.unified_msg_origin 里也有
        raw_msg = getattr(event.message_obj, "raw_message", None)
        chat_id = getattr(raw_msg, "chat_id", None) or ""
        if not chat_id:
            # 兜底：lark 私聊也用 raw chat_id（飞书自分配 oc_xxx）
            chat_id = event.get_group_id() or event.get_sender_id() or ""
        receive_id_type = "chat_id" if chat_id.startswith("oc_") else "open_id"

        brief = (event.message_str or "").strip()[:200] or "深度分析"
        # 从 event 拿 tier（llm_router 通过 set_extra 传过来的）；
        # 没拿到默认 medium（Hermes config 默认值）
        tier = event.get_extra("reasoning_tier") or "medium"
        initial_card = build_progress_card(
            title="Hermes 深度分析",
            brief=brief,
            elapsed_sec=0,
            reasoning_tier=tier,
        )

        stream = await send_card_via_runtime(
            streamer,
            card_type="thinking_waiting",
            chat_id=chat_id,
            receive_id_type=receive_id_type,
            card=initial_card,
            platform_id=event.get_platform_id() or "",
            event="start",
            detail="hermes escalation progress card",
        )
        if stream is None:
            logger.warning("[hermes_escalation] 卡片创建失败 task=%s", task.task_id[:8])
            return

        # 共享给 hermes_bridge：通过 task_id 反查 stream
        stream_map = getattr(self.context, "feishu_stream_map", None)
        if stream_map is None:
            stream_map = {}
            self.context.feishu_stream_map = stream_map  # type: ignore[attr-defined]
        stream_map[task.task_id] = {
            "platform_id": platform_id,
            "message_id": stream.message_id,
            "brief": brief,
            "reasoning_tier": tier,
        }

        # 启动后台 15 秒一次刷新计时 + 进度条
        def _builder(s):
            return build_progress_card(
                title="Hermes 深度分析",
                brief=brief,
                elapsed_sec=s.elapsed_sec,
                reasoning_tier=tier,
            )

        streamer.start_auto_update(stream.message_id, _builder, interval_sec=15.0)
        logger.info(
            "[hermes_escalation] 进度卡片已发 task=%s message_id=%s",
            task.task_id[:8],
            stream.message_id,
        )
