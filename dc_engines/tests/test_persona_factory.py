from __future__ import annotations

from pathlib import Path

from dc_engines.persona_factory import (
    PersonaFactoryStore,
    build_hermes_task_payload,
    build_persona_factory_worker_callback,
    build_research_plan,
    create_persona_request,
    evaluate_persona_factory_trigger,
    should_run_persona_factory,
    write_persona_artifacts_from_payload,
)


def test_build_research_plan_has_nuwa_lanes_and_boundaries(tmp_path: Path) -> None:
    request = create_persona_request(target="张小龙", requester_id="ou_admin")

    plan = build_research_plan(request, data_root=tmp_path)

    assert plan.target == "张小龙"
    assert len(plan.lanes) == 6
    assert [lane.lane_id for lane in plan.lanes] == [
        "writings",
        "conversations",
        "expression_dna",
        "external_views",
        "decisions",
        "timeline",
    ]
    assert plan.safety_boundary.human_review_required is True
    assert plan.safety_boundary.source_manifest_required is True
    assert plan.review_checkpoint == "human_review_required_before_registration"
    assert plan.source_manifest_path.endswith("references/source_manifest.json")
    assert str(tmp_path) in plan.workspace_dir


def test_store_persists_request_and_review_status(tmp_path: Path) -> None:
    store = PersonaFactoryStore(tmp_path / "persona_factory.db")
    request = create_persona_request(target="费曼", requester_id="ou_admin")
    plan = build_research_plan(request, data_root=tmp_path)

    submitted = store.submit(request, plan)
    loaded = store.get(submitted.request_id)

    assert loaded is not None
    assert loaded.status == "research_pending"
    assert loaded.target == "费曼"

    approved = store.set_status(submitted.request_id, "approved", reviewer="ou_admin")

    assert approved.status == "approved"
    assert store.list_recent()[0].request_id == submitted.request_id


def test_hermes_payload_requires_manifest_review_and_exports(tmp_path: Path) -> None:
    request = create_persona_request(
        target="Paul Graham",
        requester_id="ou_admin",
        output_targets=("astrbot_persona", "codex_skill"),
    )
    plan = build_research_plan(request, data_root=tmp_path)

    payload = build_hermes_task_payload(request, plan)

    assert payload["workflow_kind"] == "persona_factory"
    assert payload["engine"] == "dc_engines.persona_factory"
    assert payload["quality_gates"]["source_manifest_required"] is True
    assert payload["quality_gates"]["human_review_required_before_registration"] is True
    assert "references/source_manifest.json" in payload["required_outputs"]
    assert payload["export_targets"] == ["astrbot_persona", "codex_skill"]
    assert payload["limits"]["max_runtime_minutes"] <= 90
    assert should_run_persona_factory(payload) is True


def test_local_only_requires_local_sources() -> None:
    try:
        create_persona_request(
            target="内部专家",
            requester_id="ou_admin",
            source_mode="local_only",
        )
    except ValueError as exc:
        assert "local_source_paths" in str(exc)
    else:
        raise AssertionError("local_only without sources should fail")


def test_persona_factory_trigger_ignores_non_persona_workflows() -> None:
    decision = evaluate_persona_factory_trigger(
        {"workflow_kind": "project_followup", "engine": "dc_engines.persona_factory"}
    )

    assert decision.status == "ignored"
    assert decision.should_run is False


def test_persona_factory_trigger_rejects_missing_safety_gates(
    tmp_path: Path,
) -> None:
    request = create_persona_request(target="Grace Hopper", requester_id="ou_admin")
    plan = build_research_plan(request, data_root=tmp_path)
    payload = build_hermes_task_payload(request, plan)
    payload["quality_gates"].pop("human_review_required_before_registration")

    decision = evaluate_persona_factory_trigger(payload)

    assert decision.status == "rejected"
    assert decision.reason == "human_review_gate_missing"
    assert should_run_persona_factory(payload) is False


def test_persona_factory_trigger_rejects_unknown_export_target(
    tmp_path: Path,
) -> None:
    request = create_persona_request(target="Grace Hopper", requester_id="ou_admin")
    plan = build_research_plan(request, data_root=tmp_path)
    payload = build_hermes_task_payload(request, plan)
    payload["export_targets"] = ["astrbot_persona", "shell_script"]

    decision = evaluate_persona_factory_trigger(payload)

    assert decision.status == "rejected"
    assert decision.reason == "export_target_not_allowed"


def test_persona_factory_trigger_requires_local_only_for_non_public_target(
    tmp_path: Path,
) -> None:
    request = create_persona_request(target="内部员工小王", requester_id="ou_admin")
    plan = build_research_plan(request, data_root=tmp_path)
    payload = build_hermes_task_payload(request, plan)

    decision = evaluate_persona_factory_trigger(payload)

    assert decision.status == "rejected"
    assert decision.reason == "non_public_target_requires_local_only"


def test_persona_factory_trigger_accepts_local_only_with_sources(
    tmp_path: Path,
) -> None:
    local_source = tmp_path / "profile.md"
    local_source.write_text("内部员工公开授权材料", encoding="utf-8")
    request = create_persona_request(
        target="内部员工小王",
        requester_id="ou_admin",
        source_mode="local_only",
        local_source_paths=(str(local_source),),
    )
    plan = build_research_plan(request, data_root=tmp_path)
    payload = build_hermes_task_payload(request, plan)

    decision = evaluate_persona_factory_trigger(payload)

    assert decision.status == "accepted"
    assert decision.should_run is True


def test_write_persona_artifacts_from_hermes_payload(tmp_path: Path) -> None:
    request = create_persona_request(
        target="Grace Hopper",
        requester_id="ou_admin",
        output_targets=("astrbot_persona", "hermes_skill"),
    )
    plan = build_research_plan(request, data_root=tmp_path)
    payload = build_hermes_task_payload(request, plan)

    bundle = write_persona_artifacts_from_payload(
        payload,
        {
            "source_manifest": [
                {
                    "title": "Interview",
                    "url": "https://example.test/interview",
                    "evidence_type": "public_interview",
                }
            ],
            "research": {
                "writings": "Uses concrete examples before abstractions.",
                "timeline": "Public career milestones only.",
            },
        },
    )

    assert bundle.status == "awaiting_review"
    files = {Path(item).name for item in bundle.files}
    assert "SKILL.md" in files
    assert "source_manifest.json" in files
    assert "evaluation_report.json" in files
    assert "astrbot_persona.json" in files
    assert "hermes_skill.json" in files
    skill_text = (Path(bundle.workspace_dir) / "SKILL.md").read_text(encoding="utf-8")
    assert "not the real person" in skill_text
    report = (Path(bundle.workspace_dir) / "evaluation_report.json").read_text(
        encoding="utf-8"
    )
    assert "awaiting_review" in report


def test_build_persona_factory_worker_callback_writes_review_artifacts(
    tmp_path: Path,
) -> None:
    request = create_persona_request(target="Grace Hopper", requester_id="ou_admin")
    plan = build_research_plan(request, data_root=tmp_path)
    payload = build_hermes_task_payload(request, plan)
    payload["task_id"] = request.request_id
    payload["unified_msg_origin"] = "lark:tenant:user"

    callback = build_persona_factory_worker_callback(
        payload,
        {
            "source_manifest": [
                {
                    "title": "Public talk",
                    "url": "https://example.test/talk",
                }
            ],
            "research": {"writings": "Concrete before abstract."},
        },
    )

    assert callback["status"] == "completed"
    assert callback["workflow_kind"] == "persona_factory"
    assert callback["task_id"] == request.request_id
    assert "目录：" in callback["response"]
    bundle = callback["artifact_bundle"]
    assert bundle["status"] == "awaiting_review"
    assert (Path(bundle["workspace_dir"]) / "SKILL.md").exists()


def test_build_persona_factory_worker_callback_rejects_unsafe_payload() -> None:
    callback = build_persona_factory_worker_callback(
        {
            "workflow_kind": "persona_factory",
            "engine": "wrong",
            "request": {"request_id": "pf_bad", "target": "Grace Hopper"},
        }
    )

    assert callback["status"] == "failed"
    assert callback["error"] == "engine_mismatch"
