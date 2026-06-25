from __future__ import annotations

import importlib
import json
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

_DC_AGENT_ROOT = Path(__file__).resolve().parents[2]
_PLUGINS_PARENT = _DC_AGENT_ROOT / "data" / "plugins"
if str(_PLUGINS_PARENT) not in sys.path:
    sys.path.insert(0, str(_PLUGINS_PARENT))

_dc_router_pkg_stub = types.ModuleType("dc_router")
_dc_router_pkg_stub.__path__ = [str(_PLUGINS_PARENT / "dc_router")]  # type: ignore[attr-defined]
_dc_router_pkg_stub.__file__ = str(_PLUGINS_PARENT / "dc_router" / "__init__.py")  # type: ignore[attr-defined]
sys.modules["dc_router"] = _dc_router_pkg_stub

capture = importlib.import_module("dc_router.observation_capture")


class _Event:
    unified_msg_origin = "lark:chat:secret-session"

    def __init__(self) -> None:
        self.extras: dict[str, object] = {}

    def get_platform_id(self) -> str:
        return "巅池-Agent小助手"

    def get_sender_id(self) -> str:
        return "ou_secret_user"

    def get_extra(self, key: str, default: object = None) -> object:
        return self.extras.get(key, default)


def _decision(**metadata_updates):
    metadata = {
        "platform_id": "巅池-Agent小助手",
        "rdf_speaker_context": "employee_chat",
        "rdf_conversation_state": "followup",
        "rdf_object_reference": "generated_object",
        "rdf_action_force": "implicit",
        "rdf_department_signal": "unknown",
        "rdf_resource_need": "resource_not_required",
        "rdf_risk_gate": "block_queue",
        "rdf_next_action": "ask_clarify",
        "rule_gate_version": "1",
        "queue_allowed": "false",
        "llm_can_execute": "false",
        "rule_gate_notes": "feedback_or_followup_blocks_queue",
    }
    metadata.update(metadata_updates)
    return SimpleNamespace(
        intent="fallback",
        provider_id="aihubmix/qwen3.7-max",
        source="fallback",
        depth="direct",
        action="answer",
        metadata=metadata,
    )


def _envelope(text: str):
    return SimpleNamespace(
        text=text,
        user_id="ou_secret_user",
        session_id="lark:chat:secret-session",
    )


def test_observation_record_excludes_raw_text() -> None:
    raw_text = "头发部分不是很理想，能不能再精细一些吗？"

    record = capture.build_observation_record(
        event=_Event(),
        envelope=_envelope(raw_text),
        decision=_decision(rdf_candidate_sources="llm:queue_candidate"),
        handled=False,
        dry_run=False,
    )

    serialized = json.dumps(record, ensure_ascii=False)
    assert raw_text not in serialized
    assert record["text_hash"]
    assert record["text_shape"]["length"] == len(raw_text)
    assert record["user_hash"] != "ou_secret_user"
    assert record["session_hash"] != "lark:chat:secret-session"
    assert record["candidate_sources"] == "llm:queue_candidate"


def test_capture_appends_jsonl(tmp_path) -> None:
    target = tmp_path / "router_observations.jsonl"

    capture.capture_router_decision_observation(
        event=_Event(),
        envelope=_envelope("你好"),
        decision=_decision(),
        handled=True,
        dry_run=True,
        path=target,
    )
    capture.capture_router_decision_observation(
        event=_Event(),
        envelope=_envelope("再问一下"),
        decision=_decision(),
        handled=False,
        dry_run=False,
        path=target,
    )

    records = capture.load_observation_records(target)
    assert len(records) == 2
    assert records[0]["final"]["dry_run"] is True
    assert records[1]["final"]["handled"] is False


def test_replay_proposals_are_review_only() -> None:
    record = capture.build_observation_record(
        event=_Event(),
        envelope=_envelope("刚才那个结果还有点问题，先别排队"),
        decision=_decision(rdf_candidate_sources="llm:queue_candidate"),
        handled=False,
        dry_run=False,
    )

    proposals = capture.build_replay_proposals([record])

    assert proposals
    assert proposals[0]["status"] == "review_required"
    assert proposals[0]["reason"] == "llm_candidate_rule_gate_conflict"
    assert "text_hash" in proposals[0]
    assert "text" not in proposals[0]


def test_replay_candidate_is_sanitized_and_human_promoted() -> None:
    raw_text = "品宣和中台策略部都提到直播账号运营，这个先怎么分工？"
    record = capture.build_observation_record(
        event=_Event(),
        envelope=_envelope(raw_text),
        decision=_decision(
            rdf_conversation_state="new_request",
            rdf_department_signal="department_context",
            rdf_next_action="answer_direct",
            rule_gate_notes="department_context_not_entrypoint",
        ),
        handled=True,
        dry_run=False,
    )

    proposal = capture.build_replay_proposals([record])[0]
    candidate = proposal["replay_candidate"]
    serialized = json.dumps(candidate, ensure_ascii=False)

    assert candidate["status"] == "review_required"
    assert candidate["message_text"] is None
    assert candidate["input_text_policy"] == "human_reconstruct_from_private_context"
    assert raw_text not in serialized
    assert candidate["expected_slots"]["department_signal"] == "department_context"
    assert "rdf_department_signal" not in candidate["expected_slots"]
    assert candidate["review_actions"] == [
        "human_reconstruct_message",
        "verify_expected_slots",
        "add_to_replay_dataset_if_approved",
    ]


def test_proposal_script_writes_review_file(tmp_path) -> None:
    observations = tmp_path / "observations.jsonl"
    output = tmp_path / "proposals.json"
    record = capture.build_observation_record(
        event=_Event(),
        envelope=_envelope("你好，我想问一下今天这个事情怎么处理"),
        decision=_decision(rdf_conversation_state="new_request"),
        handled=True,
        dry_run=False,
    )
    capture.append_observation_record(record, observations)

    result = subprocess.run(
        [
            sys.executable,
            "scripts-tools/router_replay_proposals.py",
            "--observations",
            str(observations),
            "--output",
            str(output),
        ],
        cwd=_DC_AGENT_ROOT,
        text=True,
        capture_output=True,
        check=True,
    )

    proposals = json.loads(output.read_text(encoding="utf-8"))
    assert str(output) in result.stdout
    assert proposals
    assert proposals[0]["status"] == "review_required"
    assert proposals[0]["replay_candidate"]["message_text"] is None


def test_review_pack_is_redacted_and_review_only() -> None:
    raw_text = "头发部分不是很理想，能不能再精细一些吗？"
    record = capture.build_observation_record(
        event=_Event(),
        envelope=_envelope(raw_text),
        decision=_decision(rdf_candidate_sources="llm:queue_candidate"),
        handled=False,
        dry_run=False,
    )

    review_pack = capture.build_review_pack([record])
    serialized = json.dumps(review_pack, ensure_ascii=False)

    assert review_pack["status"] == "review_required"
    assert review_pack["input_observation_count"] == 1
    assert review_pack["proposal_count"] == 1
    assert review_pack["reason_counts"] == {"llm_candidate_rule_gate_conflict": 1}
    assert review_pack["review_policy"] == "manual_review_only_no_router_mutation"
    assert {
        item["surface_id"] for item in review_pack["legacy_surface_priorities"]
    } >= {
        "business_keyword_rules",
        "business_department_workflow_rules",
        "business_provider_map",
        "route_arbiter",
    }
    assert review_pack["candidates"][0]["status"] == "review_required"
    assert raw_text not in serialized
    assert review_pack["candidates"][0]["replay_candidate"]["message_text"] is None


def test_review_pack_script_writes_redacted_pack(tmp_path) -> None:
    observations = tmp_path / "observations.jsonl"
    output = tmp_path / "review_pack.json"
    record = capture.build_observation_record(
        event=_Event(),
        envelope=_envelope("你好，我想问一下今天这个事情怎么处理"),
        decision=_decision(rdf_conversation_state="new_request"),
        handled=True,
        dry_run=False,
    )
    capture.append_observation_record(record, observations)

    result = subprocess.run(
        [
            sys.executable,
            "scripts-tools/router_review_pack.py",
            "--observations",
            str(observations),
            "--output",
            str(output),
        ],
        cwd=_DC_AGENT_ROOT,
        text=True,
        capture_output=True,
        check=True,
    )

    review_pack = json.loads(output.read_text(encoding="utf-8"))
    assert str(output) in result.stdout
    assert review_pack["status"] == "review_required"
    assert review_pack["proposal_count"] == 1
    assert review_pack["legacy_surface_priorities"]
    assert review_pack["candidates"][0]["replay_candidate"]["message_text"] is None


def _approved_candidate(text: str, **updates):
    candidate = {
        "status": "approved",
        "sample_id": "approved_employee_entry_case",
        "source_observation_hash": capture._hash_text(text),
        "message_text": text,
        "metadata": {"platform_id": "巅池-Agent小助手"},
        "expected_slots": {
            "speaker_context": "employee_chat",
            "conversation_state": "new_request",
            "department_signal": "unknown",
            "next_action": "answer",
            "resource_need": "resource_not_required",
            "risk_gate": "allow",
        },
        "expected_rule_gate": {
            "queue_allowed": "false",
            "llm_can_execute": "false",
        },
        "expected_final": {
            "intent": "casual",
            "source": "keyword",
            "provider_id": "aihubmix/qwen3.7-max",
        },
    }
    candidate.update(updates)
    return candidate


def test_replay_candidate_promotion_requires_approval() -> None:
    candidate = _approved_candidate(
        "你好，我问一下这个怎么处理", status="review_required"
    )

    with pytest.raises(ValueError, match="approved"):
        capture.replay_sample_from_candidate(candidate)


def test_replay_candidate_promotion_requires_matching_hash() -> None:
    candidate = _approved_candidate(
        "你好，我问一下这个怎么处理",
        source_observation_hash="not_the_same_hash",
    )

    with pytest.raises(ValueError, match="does not match hash"):
        capture.replay_sample_from_candidate(candidate)


def test_promote_replay_candidate_appends_approved_sample(tmp_path) -> None:
    replay_path = tmp_path / "router_replay.json"
    replay_path.write_text("[]\n", encoding="utf-8")
    candidate = _approved_candidate("你好，我问一下这个怎么处理")

    sample = capture.promote_replay_candidate(candidate, replay_path=replay_path)

    payload = json.loads(replay_path.read_text(encoding="utf-8"))
    assert sample["sample_id"] == "approved_employee_entry_case"
    assert payload == [sample]
    assert payload[0]["text"] == "你好，我问一下这个怎么处理"
    assert payload[0]["expected_rule_gate"]["queue_allowed"] == "false"


def test_promote_replay_candidate_rejects_duplicate_sample_id(tmp_path) -> None:
    replay_path = tmp_path / "router_replay.json"
    candidate = _approved_candidate("你好，我问一下这个怎么处理")
    replay_path.write_text(
        json.dumps([{"sample_id": candidate["sample_id"], "text": "已有"}]),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Duplicate replay sample_id"):
        capture.promote_replay_candidate(candidate, replay_path=replay_path)


def test_promotion_script_appends_approved_candidate(tmp_path) -> None:
    replay_path = tmp_path / "router_replay.json"
    replay_path.write_text("[]\n", encoding="utf-8")
    candidate_path = tmp_path / "candidate.json"
    candidate = _approved_candidate("你好，我问一下这个怎么处理")
    candidate_path.write_text(
        json.dumps(candidate, ensure_ascii=False),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            "scripts-tools/router_promote_replay_candidate.py",
            str(candidate_path),
            "--replay",
            str(replay_path),
        ],
        cwd=_DC_AGENT_ROOT,
        text=True,
        capture_output=True,
        check=True,
    )

    payload = json.loads(replay_path.read_text(encoding="utf-8"))
    assert "approved_employee_entry_case" in result.stdout
    assert payload[0]["sample_id"] == "approved_employee_entry_case"
