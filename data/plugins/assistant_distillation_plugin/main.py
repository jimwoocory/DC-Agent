from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from dc_engines.assistant_distillation import (
    DEFAULT_DISTILLATION_DB_PATH,
    DEFAULT_LANGUAGE_OVERRIDES_PATH,
    AssistantDistillationStore,
    apply_candidate,
    approve_candidate,
    build_candidate_review_card,
    reject_candidate,
)
from dc_engines.card_runtime import send_card_via_runtime
from dc_engines.feishu_card_streamer import (
    ensure_streamers_on_context,
    extract_chat_info_from_event,
)

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.star import Context, Star, register

PLUGIN_ACTIONS = {
    "assistant_distillation_approve",
    "assistant_distillation_reject",
}


@register(
    "assistant_distillation_plugin",
    "dc_agent",
    "小助手学习候选 · 飞书管理员审批入口",
    "1.0.0",
)
class AssistantDistillationPlugin(Star):
    def __init__(self, context: Context) -> None:
        super().__init__(context)
        cfg = self._config()
        self.store = AssistantDistillationStore(
            Path(cfg.get("db_path") or DEFAULT_DISTILLATION_DB_PATH)
        )
        self.overrides_path = Path(
            cfg.get("overrides_path") or DEFAULT_LANGUAGE_OVERRIDES_PATH
        )

    def _config(self) -> dict[str, Any]:
        getter = getattr(self.context, "get_config", None)
        if callable(getter):
            cfg = getter() or {}
            return cfg if isinstance(cfg, dict) else {}
        return {}

    def _allowed_reviewers(self) -> set[str]:
        raw = self._config().get("admin_reviewers") or []
        if not isinstance(raw, list):
            return set()
        return {str(item).strip() for item in raw if str(item).strip()}

    def _max_cards(self) -> int:
        try:
            return max(1, min(10, int(self._config().get("max_cards_per_request", 5))))
        except (TypeError, ValueError):
            return 5

    def _sender_id(self, event: AstrMessageEvent) -> str:
        try:
            return str(event.get_sender_id() or "")
        except Exception:  # noqa: BLE001
            return ""

    def _reply(self, event: AstrMessageEvent, text: str) -> None:
        event.set_result(MessageEventResult().message(text).use_t2i(False).stop_event())

    async def _send_card(self, event: AstrMessageEvent, card: dict[str, Any]) -> bool:
        streamer = ensure_streamers_on_context(self.context).get(
            event.get_platform_id() or ""
        )
        chat_id, receive_id_type = extract_chat_info_from_event(event)
        if streamer is None or not chat_id:
            return False
        stream = await send_card_via_runtime(
            streamer,
            card_type="skill_review",
            chat_id=chat_id,
            receive_id_type=receive_id_type,
            card=card,
            platform_id=event.get_platform_id() or "",
            event="start",
            detail="assistant distillation review card",
        )
        return stream is not None

    def _is_reviewer(self, event: AstrMessageEvent) -> bool:
        allowed = self._allowed_reviewers()
        if not allowed:
            return False
        return self._sender_id(event) in allowed

    @filter.regex(r"^(小助手学习候选|学习候选|蒸馏候选)$")
    async def list_pending_candidates(self, event: AstrMessageEvent) -> None:
        reviewer = self._sender_id(event)
        if not self._is_reviewer(event):
            logger.warning(
                "[assistant_distillation] 非管理员请求候选 reviewer=%s", reviewer
            )
            self._reply(event, "抱歉，当前只有小助手管理员可以查看学习候选。")
            return

        candidates = self.store.list_candidates(
            status="pending",
            limit=self._max_cards(),
        )
        if not candidates:
            self._reply(event, "当前没有待审批的小助手学习候选。")
            return

        sent = 0
        for candidate in candidates:
            if await self._send_card(event, build_candidate_review_card(candidate)):
                sent += 1
        if sent:
            event.stop_event()
        else:
            lines = ["当前待审批候选："]
            lines.extend(
                f"- {item.candidate_id} · {item.kind} · {item.normalized_text or item.pattern or item.template_name}"
                for item in candidates
            )
            self._reply(event, "\n".join(lines))

    @filter.regex(r"^__card_action__:")
    async def handle_card_action(self, event: AstrMessageEvent) -> None:
        payload = self._parse_card_action(event.message_str or "")
        value = payload.get("value") if isinstance(payload, dict) else {}
        value = value if isinstance(value, dict) else {}
        action = str(value.get("action") or "")
        if action not in PLUGIN_ACTIONS:
            return

        reviewer = self._sender_id(event)
        allowed = self._allowed_reviewers()
        candidate_id = str(value.get("candidate_id") or "")
        if not candidate_id:
            self._reply(event, "候选 ID 缺失，无法处理。")
            return
        if reviewer not in allowed:
            logger.warning(
                "[assistant_distillation] 拒绝非管理员卡片操作 reviewer=%s action=%s",
                reviewer,
                action,
            )
            self._reply(event, "抱歉，当前只有小助手管理员可以审批学习候选。")
            return

        try:
            if action == "assistant_distillation_approve":
                approve_candidate(
                    self.store,
                    candidate_id,
                    reviewer=reviewer,
                    allowed_reviewers=allowed,
                )
                apply_candidate(
                    self.store,
                    candidate_id,
                    overrides_path=self.overrides_path,
                    reviewer=reviewer,
                    allowed_reviewers=allowed,
                )
                self._reply(event, "已确认并应用该学习候选，小助手会按新规则热生效。")
            elif action == "assistant_distillation_reject":
                reject_candidate(
                    self.store,
                    candidate_id,
                    reviewer=reviewer,
                    allowed_reviewers=allowed,
                    reason="feishu_card_reject",
                )
                self._reply(event, "已拒绝该学习候选，系统不会应用这条规则。")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[assistant_distillation] 卡片审批失败 candidate=%s action=%s: %s",
                candidate_id,
                action,
                exc,
            )
            self._reply(event, f"处理失败：{exc}")

    @staticmethod
    def _parse_card_action(text: str) -> dict[str, Any]:
        if not text.startswith("__card_action__:"):
            return {}
        try:
            payload = json.loads(text[len("__card_action__:") :])
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}
