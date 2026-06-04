from __future__ import annotations

import json
from pathlib import Path

from dc_engines.ai_inbox import InboxItemCreateRequest, InboxStore
from dc_engines.assistant_distillation import (
    AssistantDistillationStore,
    apply_candidate,
    approve_candidate,
    assert_reviewer_allowed,
    build_candidate_review_card,
    collect_distillation_metrics,
    generate_chitchat_candidates_from_miss_log,
    generate_intent_candidates_from_inbox,
    generate_tone_candidates_from_inbox,
    load_audit_events,
    load_language_overrides,
    reject_candidate,
    rollback_language_overrides,
    validate_language_overrides,
)


def _append_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )


def test_chitchat_misses_generate_pending_candidates(tmp_path: Path) -> None:
    miss_log = tmp_path / "misses.jsonl"
    _append_jsonl(
        miss_log,
        [
            {"normalized_text": "滴滴", "raw_text": "滴滴"},
            {"normalized_text": "滴滴", "raw_text": "滴滴"},
            {"normalized_text": "查报表", "raw_text": "查报表"},
        ],
    )
    store = AssistantDistillationStore(tmp_path / "distill.db")

    summary = generate_chitchat_candidates_from_miss_log(
        miss_log_path=miss_log,
        store=store,
        min_count=2,
    )

    assert summary["created_or_updated"] == 1
    candidates = store.list_candidates(status="pending")
    assert len(candidates) == 1
    assert candidates[0].kind == "chitchat_keyword"
    assert candidates[0].intent == "greeting"
    assert candidates[0].normalized_text == "滴滴"


def test_chitchat_candidate_generation_blocks_sensitive_and_privacy_terms(
    tmp_path: Path,
) -> None:
    miss_log = tmp_path / "misses.jsonl"
    _append_jsonl(
        miss_log,
        [
            {"normalized_text": "手机号", "raw_text": "手机号"},
            {"normalized_text": "身份证", "raw_text": "身份证"},
            {"normalized_text": "密码", "raw_text": "密码"},
            {"normalized_text": "滴滴", "raw_text": "滴滴"},
        ],
    )
    store = AssistantDistillationStore(tmp_path / "distill.db")

    summary = generate_chitchat_candidates_from_miss_log(
        miss_log_path=miss_log,
        store=store,
        min_count=1,
    )

    assert summary["created_or_updated"] == 1
    candidates = store.list_candidates(status="pending")
    assert [candidate.normalized_text for candidate in candidates] == ["滴滴"]


def test_approval_and_apply_chitchat_candidate_writes_hot_overrides(
    tmp_path: Path,
) -> None:
    store = AssistantDistillationStore(tmp_path / "distill.db")
    candidate = store.upsert_candidate(
        kind="chitchat_keyword",
        intent="greeting",
        normalized_text="嗨喽",
        response_text="您好，我在的。您直接发需求就好。",
        source="test",
        evidence=[{"raw_text": "嗨喽"}],
        count=3,
    )

    approve_candidate(store, candidate.candidate_id, reviewer="ops")
    apply_candidate(
        store,
        candidate.candidate_id,
        overrides_path=tmp_path / "assistant_language_overrides.json",
        reviewer="ops",
    )

    overrides = load_language_overrides(tmp_path / "assistant_language_overrides.json")
    assert "嗨喽" in overrides["chitchat"]["keywords"]["greeting"]
    assert (
        "您好，我在的。您直接发需求就好。"
        in overrides["chitchat"]["responses"]["greeting"]
    )
    applied = store.get_candidate(candidate.candidate_id)
    assert applied is not None
    assert applied.status == "applied"
    audit_events = load_audit_events(store, candidate_id=candidate.candidate_id)
    assert [event.action for event in audit_events] == ["approved", "applied"]
    assert audit_events[-1].affected_rules == ["chitchat.greeting.嗨喽"]


def test_apply_candidate_writes_versioned_atomic_config_and_backup(
    tmp_path: Path,
) -> None:
    store = AssistantDistillationStore(tmp_path / "distill.db")
    overrides_path = tmp_path / "assistant_language_overrides.json"
    overrides_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "version": 7,
                "chitchat": {"keywords": {}, "responses": {}},
                "intent_aliases": [],
                "tone_templates": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    candidate = store.upsert_candidate(
        kind="intent_alias",
        intent="writing",
        pattern="短一点",
        source="test",
        evidence=[],
        count=1,
    )

    approve_candidate(store, candidate.candidate_id, reviewer="ops")
    apply_candidate(
        store,
        candidate.candidate_id,
        overrides_path=overrides_path,
        reviewer="ops",
    )

    data = json.loads(overrides_path.read_text(encoding="utf-8"))
    assert data["schema_version"] == 1
    assert data["version"] == 8
    assert data["updated_at"]
    assert data["intent_aliases"][0]["pattern"] == "短一点"
    backups = list(tmp_path.glob("assistant_language_overrides.json.rollback-*"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text(encoding="utf-8"))["version"] == 7


def test_invalid_overrides_fail_schema_validation() -> None:
    try:
        validate_language_overrides(
            {
                "schema_version": 1,
                "version": 1,
                "chitchat": {"keywords": {"greeting": "不是数组"}, "responses": {}},
                "intent_aliases": [],
                "tone_templates": [],
            }
        )
    except ValueError as exc:
        assert "chitchat.keywords.greeting" in str(exc)
    else:
        raise AssertionError("invalid override schema should fail")


def test_rollback_restores_previous_overrides(tmp_path: Path) -> None:
    overrides_path = tmp_path / "assistant_language_overrides.json"
    rollback_path = tmp_path / "assistant_language_overrides.json.rollback-old"
    overrides_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "version": 2,
                "chitchat": {"keywords": {"greeting": ["滴滴"]}, "responses": {}},
                "intent_aliases": [],
                "tone_templates": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    rollback_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "version": 1,
                "chitchat": {"keywords": {}, "responses": {}},
                "intent_aliases": [],
                "tone_templates": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    rollback_language_overrides(overrides_path, rollback_path)

    restored = json.loads(overrides_path.read_text(encoding="utf-8"))
    assert restored["version"] == 3
    assert restored["chitchat"]["keywords"] == {}


def test_review_permission_and_card_payload(tmp_path: Path) -> None:
    store = AssistantDistillationStore(tmp_path / "distill.db")
    candidate = store.upsert_candidate(
        kind="chitchat_keyword",
        intent="greeting",
        normalized_text="滴滴",
        source="test",
        evidence=[],
        count=3,
    )

    assert_reviewer_allowed("ou_admin", {"ou_admin"})
    try:
        assert_reviewer_allowed("ou_staff", {"ou_admin"})
    except PermissionError as exc:
        assert "not allowed" in str(exc)
    else:
        raise AssertionError("non-admin reviewer should be rejected")

    card = build_candidate_review_card(candidate)
    content = json.dumps(card, ensure_ascii=False)
    assert "assistant_distillation_approve" in content
    assert "assistant_distillation_reject" in content
    assert candidate.candidate_id in content


def test_collect_distillation_metrics(tmp_path: Path) -> None:
    miss_log = tmp_path / "misses.jsonl"
    _append_jsonl(
        miss_log,
        [
            {"normalized_text": "滴滴", "source": "llm_router_chitchat_guard"},
            {"normalized_text": "嗨喽", "source": "llm_router_chitchat_guard"},
        ],
    )
    store = AssistantDistillationStore(tmp_path / "distill.db")
    candidate = store.upsert_candidate(
        kind="chitchat_keyword",
        intent="greeting",
        normalized_text="滴滴",
        source="test",
        evidence=[],
        count=2,
    )
    approve_candidate(store, candidate.candidate_id, reviewer="ops")

    hit_log = tmp_path / "hits.jsonl"
    _append_jsonl(hit_log, [{"normalized_text": "你好"}, {"normalized_text": "在吗"}])

    metrics = collect_distillation_metrics(
        store=store,
        miss_log_path=miss_log,
        hit_log_path=hit_log,
    )

    assert metrics["candidate_status_counts"]["approved"] == 1
    assert metrics["pending_approval_count"] == 0
    assert metrics["chitchat_miss_count"] == 2
    assert metrics["chitchat_hit_count"] == 2
    assert metrics["chitchat_hit_rate"] == 0.5
    assert metrics["llm_saved_estimate_count"] == 2
    assert metrics["estimated_false_trigger_rate"] == 0.0
    assert metrics["approval_backlog_count"] == 0


def test_rejected_candidate_cannot_apply(tmp_path: Path) -> None:
    store = AssistantDistillationStore(tmp_path / "distill.db")
    candidate = store.upsert_candidate(
        kind="intent_alias",
        intent="writing",
        pattern="短一点",
        source="test",
        evidence=[],
        count=1,
    )

    reject_candidate(store, candidate.candidate_id, reviewer="ops")

    try:
        apply_candidate(
            store, candidate.candidate_id, overrides_path=tmp_path / "o.json"
        )
    except ValueError as exc:
        assert "approved" in str(exc)
    else:
        raise AssertionError("rejected candidates must not be applied")


async def test_inbox_feedback_generates_tone_and_intent_candidates(
    tmp_path: Path,
) -> None:
    inbox = InboxStore(tmp_path / "ai_inbox.db")
    await inbox.initialize()
    await inbox.create_item(
        InboxItemCreateRequest(
            session_id="s1",
            conversation_id="c1",
            platform_id="巅池-Agent小助手",
            sender_id="u1",
            sender_name="测试同事",
            text="刚刚那句太官方了，不够人性化",
            category="feedback",
        )
    )
    await inbox.create_item(
        InboxItemCreateRequest(
            session_id="s2",
            conversation_id="c2",
            platform_id="巅池-Agent小助手",
            sender_id="u2",
            sender_name="测试同事",
            text="员工问怎么把你拉进群聊，你要能识别",
            category="feedback",
        )
    )
    store = AssistantDistillationStore(tmp_path / "distill.db")

    tone_summary = generate_tone_candidates_from_inbox(
        inbox_db_path=tmp_path / "ai_inbox.db",
        store=store,
    )
    intent_summary = generate_intent_candidates_from_inbox(
        inbox_db_path=tmp_path / "ai_inbox.db",
        store=store,
    )

    assert tone_summary["created_or_updated"] == 1
    assert intent_summary["created_or_updated"] == 1
    candidates = store.list_candidates(status="pending")
    assert {candidate.kind for candidate in candidates} == {
        "tone_template",
        "intent_alias",
    }
