from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from dc_engines.card_runtime import send_card_via_runtime
from dc_engines.department_workflows.content_rule_overrides import (
    DEFAULT_RULE_OVERRIDES_PATH,
)
from dc_engines.department_workflows.content_rule_proposals import (
    DEFAULT_RULE_PROPOSALS_DB_PATH,
    ContentSopRuleProposal,
    ContentSopRuleProposalStore,
    apply_approved_rule_proposal,
    approve_rule_proposal,
    build_rule_proposal_review_card,
    reject_rule_proposal,
    rollback_applied_rule_proposal,
)
from dc_engines.department_workflows.content_sop_ops import (
    DEFAULT_GOVERNED_MEMORY_DB_PATH,
    build_content_sop_ops_dashboard,
    build_content_sop_ops_reminder_card,
    build_content_sop_ops_reminders,
)
from dc_engines.feishu_card_streamer import (
    ensure_streamers_on_context,
    extract_chat_info_from_event,
)

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.star import Context, Star, register

CARD_SOURCE = "content_sop_rule_review"
PLUGIN_ACTIONS = {
    "content_sop_rule_approve_apply",
    "content_sop_rule_reject",
    "content_sop_rule_rollback",
    "content_sop_rule_apply_confirm",
    "content_sop_rule_rollback_confirm",
    "content_sop_rule_cancel",
}


@register(
    "content_sop_rule_review_plugin",
    "dc_agent",
    "内容 SOP 规则候选 · 飞书管理员审批入口",
    "0.1.0",
)
class ContentSopRuleReviewPlugin(Star):
    def __init__(self, context: Context) -> None:
        super().__init__(context)
        cfg = self._config()
        self.store = ContentSopRuleProposalStore(
            Path(cfg.get("db_path") or DEFAULT_RULE_PROPOSALS_DB_PATH)
        )
        self.overrides_path = Path(
            cfg.get("rule_overrides_path") or DEFAULT_RULE_OVERRIDES_PATH
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

    def _is_reviewer(self, event: AstrMessageEvent) -> bool:
        allowed = self._allowed_reviewers()
        if not allowed:
            return False
        return self._sender_id(event) in allowed

    def _is_trusted_card_action(self, event: AstrMessageEvent) -> bool:
        msg = getattr(event, "message_obj", None)
        return (
            getattr(event, "is_card_action", False) is True
            or getattr(msg, "is_card_action", False) is True
        )

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
            detail="content SOP rule review card",
        )
        return stream is not None

    @filter.regex(r"^(内容SOP规则候选|内容 SOP 规则候选|SOP规则候选)$")
    async def list_pending_proposals(self, event: AstrMessageEvent) -> None:
        reviewer = self._sender_id(event)
        if not self._is_reviewer(event):
            logger.warning(
                "[content_sop_rule_review] 非管理员请求候选 reviewer=%s",
                reviewer,
            )
            self._reply(event, "抱歉，当前只有内容 SOP 管理员可以查看规则候选。")
            return

        proposals = self.store.list_proposals(
            status="pending",
            limit=self._max_cards(),
        )
        if not proposals:
            self._reply(event, "当前没有待审批的内容 SOP 规则候选。")
            return

        sent = 0
        for proposal in proposals:
            if await self._send_card(event, build_rule_proposal_review_card(proposal)):
                sent += 1
        if sent:
            event.stop_event()
            return
        self._reply(event, _fallback_proposal_list(proposals))

    @filter.regex(r"^(内容SOP运营提醒|内容 SOP 运营提醒|SOP运营提醒)$")
    async def send_ops_reminders(self, event: AstrMessageEvent) -> None:
        reviewer = self._sender_id(event)
        if not self._is_reviewer(event):
            logger.warning(
                "[content_sop_rule_review] 非管理员请求运营提醒 reviewer=%s",
                reviewer,
            )
            self._reply(event, "抱歉，当前只有内容 SOP 管理员可以查看运营提醒。")
            return

        cfg = self._config()
        governed_memory_db_path = Path(
            cfg.get("governed_memory_db_path") or DEFAULT_GOVERNED_MEMORY_DB_PATH
        )
        dashboard = build_content_sop_ops_dashboard(
            proposal_store=self.store,
            overrides_path=self.overrides_path,
            governed_memory_db_path=governed_memory_db_path,
        )
        reminders = build_content_sop_ops_reminders(dashboard)
        card = build_content_sop_ops_reminder_card(reminders)
        if await self._send_card(event, card):
            event.stop_event()
            return
        if reminders:
            self._reply(
                event,
                "\n".join(
                    ["当前内容 SOP 运营提醒："]
                    + [f"- {item.title}：{item.message}" for item in reminders]
                ),
            )
            return
        self._reply(event, "当前没有需要处理的内容 SOP 运营提醒。")

    @filter.regex(r"^__card_action__:")
    async def handle_card_action(self, event: AstrMessageEvent) -> None:
        payload = self._parse_card_action(event.message_str or "")
        value = payload.get("value") if isinstance(payload, dict) else {}
        value = value if isinstance(value, dict) else {}
        if value.get("source") != CARD_SOURCE:
            return
        action = str(value.get("action") or "")
        if action not in PLUGIN_ACTIONS:
            return
        if not self._is_trusted_card_action(event):
            logger.warning(
                "[content_sop_rule_review] 拒绝非可信卡片动作 sender=%s action=%s",
                self._sender_id(event),
                action,
            )
            self._reply(event, "拒绝处理：该操作不是可信飞书卡片回调。")
            return

        reviewer = self._sender_id(event)
        allowed = self._allowed_reviewers()
        if reviewer not in allowed:
            logger.warning(
                "[content_sop_rule_review] 拒绝非管理员卡片操作 reviewer=%s action=%s",
                reviewer,
                action,
            )
            self._reply(event, "抱歉，当前只有内容 SOP 管理员可以审批规则候选。")
            return

        proposal_id = str(value.get("proposal_id") or "")
        if not proposal_id:
            self._reply(event, "候选 ID 缺失，无法处理。")
            return

        try:
            await self._handle_action(event, action, proposal_id, reviewer, allowed)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[content_sop_rule_review] 卡片审批失败 proposal=%s action=%s: %s",
                proposal_id,
                action,
                exc,
            )
            self._reply(event, f"处理失败：{exc}")

    async def _handle_action(
        self,
        event: AstrMessageEvent,
        action: str,
        proposal_id: str,
        reviewer: str,
        allowed: set[str],
    ) -> None:
        if action == "content_sop_rule_approve_apply":
            proposal = approve_rule_proposal(
                self.store,
                proposal_id,
                reviewer=reviewer,
                allowed_reviewers=allowed,
            )
            await self._send_card(event, _confirm_card(proposal, "apply"))
            self._reply(event, "已批准该候选，请在确认卡中二次确认后应用。")
        elif action == "content_sop_rule_reject":
            reject_rule_proposal(
                self.store,
                proposal_id,
                reviewer=reviewer,
                allowed_reviewers=allowed,
                reason="feishu_card_reject",
            )
            self._reply(event, "已拒绝该内容 SOP 规则候选，不会应用到运行时。")
        elif action == "content_sop_rule_rollback":
            proposal = self.store.get_proposal(proposal_id)
            if proposal is None:
                raise LookupError(f"proposal {proposal_id!r} not found")
            await self._send_card(event, _confirm_card(proposal, "rollback"))
            self._reply(event, "请在确认卡中二次确认后回滚该运行时规则。")
        elif action == "content_sop_rule_apply_confirm":
            result = apply_approved_rule_proposal(
                self.store,
                proposal_id,
                overrides_path=self.overrides_path,
                reviewer=reviewer,
                allowed_reviewers=allowed,
            )
            self._reply(event, f"已应用内容 SOP 规则：{result.rule_id}")
        elif action == "content_sop_rule_rollback_confirm":
            result = rollback_applied_rule_proposal(
                self.store,
                proposal_id,
                overrides_path=self.overrides_path,
                reviewer=reviewer,
                allowed_reviewers=allowed,
            )
            self._reply(event, f"已回滚内容 SOP 规则：{result.rule_id}")
        elif action == "content_sop_rule_cancel":
            self._reply(event, "已取消。")

    @staticmethod
    def _parse_card_action(text: str) -> dict[str, Any]:
        if not text.startswith("__card_action__:"):
            return {}
        try:
            payload = json.loads(text[len("__card_action__:") :])
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}


def _confirm_card(proposal: ContentSopRuleProposal, operation: str) -> dict[str, Any]:
    if operation == "apply":
        title = "确认应用内容 SOP 规则"
        confirm_action = "content_sop_rule_apply_confirm"
        primary_text = "确认应用"
    else:
        title = "确认回滚内容 SOP 规则"
        confirm_action = "content_sop_rule_rollback_confirm"
        primary_text = "确认回滚"
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "red" if operation == "rollback" else "green",
            "title": {"tag": "plain_text", "content": title},
        },
        "elements": [
            {
                "tag": "markdown",
                "content": (
                    f"**proposal_id**: `{proposal.proposal_id}`\n"
                    f"**部门**: {proposal.department_id}\n"
                    f"**场景**: {proposal.scenario_id or '全部'}\n"
                    f"**规则**: {proposal.rule_text}"
                ),
            },
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": primary_text},
                        "type": "primary",
                        "value": {
                            "source": CARD_SOURCE,
                            "action": confirm_action,
                            "proposal_id": proposal.proposal_id,
                        },
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "取消"},
                        "value": {
                            "source": CARD_SOURCE,
                            "action": "content_sop_rule_cancel",
                            "proposal_id": proposal.proposal_id,
                        },
                    },
                ],
            },
        ],
    }


def _fallback_proposal_list(proposals: list[ContentSopRuleProposal]) -> str:
    lines = ["当前待审批内容 SOP 规则候选："]
    lines.extend(
        f"- {item.proposal_id} · {item.department_id}/{item.scenario_id or 'all'} · {item.rule_text}"
        for item in proposals
    )
    return "\n".join(lines)
