from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from dc_engines.department_workflows.content_rule_proposals import (
    ContentSopRuleProposalStore,
)


class FakeContext:
    def __init__(self, cfg: dict) -> None:
        self._cfg = cfg

    def get_config(self):
        return self._cfg


class FakeEvent:
    def __init__(
        self,
        *,
        sender_id: str = "ou_admin",
        message_str: str = "",
        is_card_action: bool = False,
        requester_dc_permissions: list[dict] | None = None,
    ) -> None:
        self.message_str = message_str
        self.is_card_action = is_card_action
        self.message_obj = SimpleNamespace(is_card_action=is_card_action)
        self._sender_id = sender_id
        self.requester_dc_permissions = requester_dc_permissions or []
        self.results: list[str] = []
        self.stopped = False

    def get_sender_id(self):
        return self._sender_id

    def get_extra(self, key: str, default=None):
        if key == "requester_dc_permissions":
            return self.requester_dc_permissions
        return default

    def get_platform_id(self):
        return "巅池-Agent小助手"

    def set_result(self, result):
        self.results.append(str(getattr(result, "chain", "") or result))

    def stop_event(self):
        self.stopped = True


def _proposal() -> dict:
    return {
        "proposal_id": "proposal_no_email",
        "department_id": "client_dept",
        "scenario_id": "customer_greeting",
        "rule_type": "process",
        "rule_text": "客户触达默认使用飞书和私域，不默认使用邮件。",
        "support_count": 3,
        "evidence_candidate_ids": ["cand_1", "cand_2", "cand_3"],
        "status": "pending",
    }


def _card_action(action: str, proposal_id: str = "proposal_no_email") -> str:
    return "__card_action__:" + json.dumps(
        {
            "value": {
                "source": "content_sop_rule_review",
                "action": action,
                "proposal_id": proposal_id,
            }
        },
        ensure_ascii=False,
    )


@pytest.mark.asyncio
async def test_content_sop_rule_plugin_rejects_untrusted_card_action(tmp_path: Path):
    from data.plugins.content_sop_rule_review_plugin.main import (
        ContentSopRuleReviewPlugin,
    )

    plugin = ContentSopRuleReviewPlugin(
        FakeContext(
            {
                "admin_reviewers": ["ou_admin"],
                "db_path": str(tmp_path / "proposals.db"),
                "rule_overrides_path": str(tmp_path / "overrides.json"),
            }
        )
    )
    event = FakeEvent(
        sender_id="ou_admin",
        message_str=_card_action("content_sop_rule_approve_apply"),
        is_card_action=False,
    )

    await plugin.handle_card_action(event)

    assert any("不是可信飞书卡片回调" in item for item in event.results)


@pytest.mark.asyncio
async def test_content_sop_rule_plugin_requires_admin_reviewer(tmp_path: Path):
    from data.plugins.content_sop_rule_review_plugin.main import (
        ContentSopRuleReviewPlugin,
    )

    plugin = ContentSopRuleReviewPlugin(
        FakeContext(
            {
                "admin_reviewers": ["ou_admin"],
                "db_path": str(tmp_path / "proposals.db"),
                "rule_overrides_path": str(tmp_path / "overrides.json"),
            }
        )
    )
    event = FakeEvent(
        sender_id="ou_user",
        message_str=_card_action("content_sop_rule_approve_apply"),
        is_card_action=True,
    )

    await plugin.handle_card_action(event)

    assert any("只有内容 SOP 管理员" in item for item in event.results)


@pytest.mark.asyncio
async def test_content_sop_rule_plugin_accepts_explicit_dc_review_permission(
    tmp_path: Path,
):
    from data.plugins.content_sop_rule_review_plugin.main import (
        ContentSopRuleReviewPlugin,
    )

    plugin = ContentSopRuleReviewPlugin(
        FakeContext(
            {
                "admin_reviewers": [],
                "db_path": str(tmp_path / "proposals.db"),
                "rule_overrides_path": str(tmp_path / "overrides.json"),
            }
        )
    )
    event = FakeEvent(
        sender_id="ou_ops",
        message_str=_card_action("content_sop_rule_reject"),
        is_card_action=True,
        requester_dc_permissions=[
            {
                "subject_id": "ou_ops",
                "subject_type": "user",
                "permission": "content_rule_review",
                "scope": "*",
                "source": "dc_permission_assignments",
                "enabled": True,
            }
        ],
    )

    assert plugin._is_reviewer(event) is True


@pytest.mark.asyncio
async def test_content_sop_rule_plugin_rejects_permission_bound_to_another_subject(
    tmp_path: Path,
):
    from data.plugins.content_sop_rule_review_plugin.main import (
        ContentSopRuleReviewPlugin,
    )

    plugin = ContentSopRuleReviewPlugin(
        FakeContext(
            {
                "admin_reviewers": [],
                "db_path": str(tmp_path / "proposals.db"),
                "rule_overrides_path": str(tmp_path / "overrides.json"),
            }
        )
    )
    event = FakeEvent(
        sender_id="ou_user",
        requester_dc_permissions=[
            {
                "subject_id": "ou_ops",
                "subject_type": "user",
                "permission": "content_rule_review",
                "scope": "*",
                "source": "manual",
                "enabled": True,
            }
        ],
    )

    assert plugin._is_reviewer(event) is False


@pytest.mark.asyncio
async def test_content_sop_rule_plugin_approve_then_confirm_apply(tmp_path: Path):
    from data.plugins.content_sop_rule_review_plugin.main import (
        ContentSopRuleReviewPlugin,
    )

    db_path = tmp_path / "proposals.db"
    overrides_path = tmp_path / "overrides.json"
    store = ContentSopRuleProposalStore(db_path)
    store.upsert_proposal(_proposal())
    plugin = ContentSopRuleReviewPlugin(
        FakeContext(
            {
                "admin_reviewers": ["ou_admin"],
                "db_path": str(db_path),
                "rule_overrides_path": str(overrides_path),
            }
        )
    )
    sent_cards: list[dict] = []

    async def fake_send_card(_event, card):
        sent_cards.append(card)
        return True

    plugin._send_card = fake_send_card

    await plugin.handle_card_action(
        FakeEvent(
            message_str=_card_action("content_sop_rule_approve_apply"),
            is_card_action=True,
        )
    )
    await plugin.handle_card_action(
        FakeEvent(
            message_str=_card_action("content_sop_rule_apply_confirm"),
            is_card_action=True,
        )
    )

    reloaded = store.get_proposal("proposal_no_email")
    assert sent_cards
    assert reloaded is not None
    assert reloaded.status == "applied"
    assert overrides_path.exists()


@pytest.mark.asyncio
async def test_content_sop_rule_plugin_rollback_confirm(tmp_path: Path):
    from data.plugins.content_sop_rule_review_plugin.main import (
        ContentSopRuleReviewPlugin,
    )

    db_path = tmp_path / "proposals.db"
    overrides_path = tmp_path / "overrides.json"
    store = ContentSopRuleProposalStore(db_path)
    proposal = store.upsert_proposal(_proposal())
    from dc_engines.department_workflows.content_rule_proposals import (
        apply_approved_rule_proposal,
        approve_rule_proposal,
    )

    approve_rule_proposal(store, proposal.proposal_id, reviewer="ou_admin")
    apply_approved_rule_proposal(
        store,
        proposal.proposal_id,
        overrides_path=overrides_path,
        reviewer="ou_admin",
    )
    plugin = ContentSopRuleReviewPlugin(
        FakeContext(
            {
                "admin_reviewers": ["ou_admin"],
                "db_path": str(db_path),
                "rule_overrides_path": str(overrides_path),
            }
        )
    )

    await plugin.handle_card_action(
        FakeEvent(
            message_str=_card_action("content_sop_rule_rollback_confirm"),
            is_card_action=True,
        )
    )

    reloaded = store.get_proposal("proposal_no_email")
    assert reloaded is not None
    assert reloaded.status == "rolled_back"


@pytest.mark.asyncio
async def test_content_sop_rule_plugin_sends_ops_reminder_card(tmp_path: Path):
    from data.plugins.content_sop_rule_review_plugin.main import (
        ContentSopRuleReviewPlugin,
    )

    db_path = tmp_path / "proposals.db"
    store = ContentSopRuleProposalStore(db_path)
    store.upsert_proposal(_proposal())
    plugin = ContentSopRuleReviewPlugin(
        FakeContext(
            {
                "admin_reviewers": ["ou_admin"],
                "db_path": str(db_path),
                "rule_overrides_path": str(tmp_path / "overrides.json"),
                "governed_memory_db_path": str(tmp_path / "governed_memory.db"),
            }
        )
    )
    sent_cards: list[dict] = []

    async def fake_send_card(_event, card):
        sent_cards.append(card)
        return True

    plugin._send_card = fake_send_card
    event = FakeEvent(sender_id="ou_admin")

    await plugin.send_ops_reminders(event)

    assert event.stopped is True
    assert sent_cards[0]["header"]["title"]["content"] == "内容 SOP 运营提醒"
    assert "规则候选待审批" in sent_cards[0]["elements"][0]["content"]
