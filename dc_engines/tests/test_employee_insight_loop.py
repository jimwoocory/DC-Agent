from dc_engines.employee_insight_loop import (
    CandidateType,
    EmployeeInsightCandidate,
    EmployeeInsightDashboardSnapshot,
    EmployeeInsightObsidianExporter,
    EmployeeInsightSession,
    EmployeeInsightSessionStatus,
    EmployeeInsightStore,
    HermesDeepDivePolicy,
    InsightEvent,
    ReviewStatus,
    TaskStatus,
    build_candidate_from_session,
    render_obsidian_candidate_note,
    transition_session,
    transition_task,
)


def test_session_state_machine_allows_private_dm_task_path() -> None:
    session = EmployeeInsightSession(
        session_id="sess_001",
        employee_id="ou_001",
        channel="lark_dm",
        trigger_type="daily_outreach",
    )

    sent = transition_session(session, EmployeeInsightSessionStatus.SENT)
    opened = transition_session(sent, EmployeeInsightSessionStatus.OPENED)
    engaged = transition_session(opened, EmployeeInsightSessionStatus.ENGAGED)
    completed = transition_session(engaged, EmployeeInsightSessionStatus.COMPLETED)

    assert completed.status == EmployeeInsightSessionStatus.COMPLETED


def test_session_state_machine_rejects_completion_before_engagement() -> None:
    session = EmployeeInsightSession(
        session_id="sess_001",
        employee_id="ou_001",
        channel="lark_dm",
        trigger_type="daily_outreach",
    )

    try:
        transition_session(session, EmployeeInsightSessionStatus.COMPLETED)
    except ValueError as exc:
        assert "illegal session transition" in str(exc)
    else:
        raise AssertionError("expected illegal transition to fail")


def test_task_state_machine_requires_satisfaction_before_completion() -> None:
    assert (
        transition_task(TaskStatus.RESULT_DELIVERED, TaskStatus.COMPLETED)
        == TaskStatus.COMPLETED
    )

    try:
        transition_task(TaskStatus.RUNNING, TaskStatus.COMPLETED)
    except ValueError as exc:
        assert "illegal task transition" in str(exc)
    else:
        raise AssertionError("expected illegal transition to fail")


def test_candidate_defaults_to_review_required_and_does_not_patch_runtime() -> None:
    candidate = EmployeeInsightCandidate(
        candidate_id="cand_001",
        candidate_type=CandidateType.TEMPLATE,
        source_session_ids=["sess_001"],
        department_id="planning",
        scenario_id="write_notice",
        title="通知模板需要公司内部话术",
        summary="员工觉得生成结果不像内部通知。",
        evidence=[{"event_id": "evt_001", "text": "不像我们平时发的通知"}],
        confidence=0.82,
    )

    assert candidate.review_status == ReviewStatus.REVIEW_REQUIRED
    assert candidate.is_runtime_eligible is False


def test_approved_candidate_is_runtime_eligible_when_not_sensitive() -> None:
    candidate = EmployeeInsightCandidate(
        candidate_id="cand_001",
        candidate_type=CandidateType.ROUTER,
        source_session_ids=["sess_001"],
        department_id="planning",
        scenario_id="unknown_how_to_start",
        title="不知道怎么用需要路由到新手引导",
        summary="员工表达不知道怎么问时，应进入新手陪跑。",
        evidence=[{"event_id": "evt_001", "text": "我不知道怎么用"}],
        confidence=0.91,
        review_status=ReviewStatus.APPROVED,
        sensitivity="internal",
    )

    assert candidate.is_runtime_eligible is True


def test_sensitive_candidate_is_never_runtime_eligible() -> None:
    candidate = EmployeeInsightCandidate(
        candidate_id="cand_001",
        candidate_type=CandidateType.KNOWLEDGE_GAP,
        source_session_ids=["sess_001"],
        department_id="client",
        scenario_id="customer_issue",
        title="客户资料缺口",
        summary="员工反馈客户资料缺失。",
        evidence=[{"event_id": "evt_001", "text": "这里有客户隐私"}],
        confidence=0.7,
        review_status=ReviewStatus.APPROVED,
        sensitivity="sensitive_blocked",
    )

    assert candidate.is_runtime_eligible is False


def test_build_candidate_from_session_uses_friction_points_as_evidence() -> None:
    session = EmployeeInsightSession(
        session_id="sess_001",
        employee_id="ou_001",
        channel="lark_dm",
        trigger_type="daily_outreach",
        department_id="planning",
        scenario_id="write_notice",
        original_request="帮我写一个培训通知",
        normalized_goal="生成内部培训通知",
        friction_points=["tone_not_internal", "format_mismatch"],
        status=EmployeeInsightSessionStatus.COMPLETED,
    )
    events = [
        InsightEvent(
            event_id="evt_001",
            session_id="sess_001",
            event_type="satisfaction_recorded",
            actor="employee",
            payload={
                "satisfaction": "needs_revision",
                "text": "内容能用，但不像我们内部通知格式。",
            },
        )
    ]

    candidate = build_candidate_from_session(session, events)

    assert candidate.candidate_type == CandidateType.TEMPLATE
    assert candidate.review_status == ReviewStatus.REVIEW_REQUIRED
    assert candidate.source_session_ids == ["sess_001"]
    assert candidate.scenario_id == "write_notice"
    assert "tone_not_internal" in candidate.summary
    assert candidate.evidence[0]["event_id"] == "evt_001"


async def test_store_persists_sessions_events_candidates_and_audit(tmp_path) -> None:
    store = EmployeeInsightStore(tmp_path / "employee_insight.db")
    session = EmployeeInsightSession(
        session_id="sess_001",
        employee_id="ou_001",
        channel="lark_dm",
        trigger_type="daily_outreach",
        department_id="planning",
        scenario_id="write_notice",
        status=EmployeeInsightSessionStatus.ENGAGED,
    )
    event = InsightEvent(
        event_id="evt_001",
        session_id="sess_001",
        event_type="task_started",
        actor="employee",
        payload={"text": "帮我写通知"},
    )
    candidate = EmployeeInsightCandidate(
        candidate_id="cand_001",
        candidate_type=CandidateType.TEMPLATE,
        source_session_ids=["sess_001"],
        department_id="planning",
        scenario_id="write_notice",
        title="通知模板优化",
        summary="员工需要内部通知格式。",
        evidence=[{"event_id": "evt_001"}],
        confidence=0.8,
    )

    await store.upsert_session(session)
    await store.append_event(event)
    await store.upsert_candidate(candidate)
    await store.record_audit(
        action="governance_exported",
        actor="system",
        target_id="cand_001",
        detail={"path": "Inbox/cand_001.md"},
    )

    assert (await store.get_session("sess_001")).status == (
        EmployeeInsightSessionStatus.ENGAGED
    )
    assert [item.event_id for item in await store.list_events("sess_001")] == [
        "evt_001"
    ]
    assert (await store.get_candidate("cand_001")).review_status == (
        ReviewStatus.REVIEW_REQUIRED
    )
    assert (await store.list_audit_events("cand_001"))[0].action == (
        "governance_exported"
    )


def test_render_obsidian_candidate_note_contains_required_frontmatter() -> None:
    candidate = EmployeeInsightCandidate(
        candidate_id="cand_001",
        candidate_type=CandidateType.TEMPLATE,
        source_session_ids=["sess_001"],
        department_id="planning",
        scenario_id="write_notice",
        title="通知模板优化",
        summary="员工需要内部通知格式。",
        evidence=[{"event_id": "evt_001", "text": "不像内部通知"}],
        confidence=0.8,
    )

    note = render_obsidian_candidate_note(candidate)

    assert note.startswith("---\n")
    assert "insight_id: cand_001" in note
    assert "review_status: need_review" in note
    assert "candidate_type: template_candidate" in note
    assert "sensitivity: internal" in note
    assert "## 员工原始表达与证据" in note
    assert "## 审批意见" in note


async def test_obsidian_exporter_writes_inbox_note_and_updates_candidate(
    tmp_path,
) -> None:
    store = EmployeeInsightStore(tmp_path / "employee_insight.db")
    candidate = EmployeeInsightCandidate(
        candidate_id="cand_001",
        candidate_type=CandidateType.TEMPLATE,
        source_session_ids=["sess_001"],
        department_id="planning",
        scenario_id="write_notice",
        title="通知模板优化",
        summary="员工需要内部通知格式。",
        evidence=[{"event_id": "evt_001", "text": "不像内部通知"}],
        confidence=0.8,
    )
    await store.upsert_candidate(candidate)
    exporter = EmployeeInsightObsidianExporter(tmp_path / "ObsidianVault")

    exported = await exporter.export_candidate(store, candidate, actor="tester")

    note_path = tmp_path / exported.obsidian_note_path
    assert note_path.exists()
    assert "insight_id: cand_001" in note_path.read_text(encoding="utf-8")
    stored = await store.get_candidate("cand_001")
    assert stored.obsidian_note_path == exported.obsidian_note_path
    audit = await store.list_audit_events("cand_001")
    assert audit[-1].action == "governance_exported"


async def test_dashboard_snapshot_aggregates_operational_metrics(tmp_path) -> None:
    store = EmployeeInsightStore(tmp_path / "employee_insight.db")
    await store.upsert_session(
        EmployeeInsightSession(
            session_id="sess_001",
            employee_id="ou_001",
            channel="lark_dm",
            trigger_type="daily_outreach",
            department_id="planning",
            scenario_id="write_notice",
            status=EmployeeInsightSessionStatus.COMPLETED,
        )
    )
    await store.upsert_session(
        EmployeeInsightSession(
            session_id="sess_002",
            employee_id="ou_002",
            channel="lark_dm",
            trigger_type="daily_outreach",
            department_id="client",
            scenario_id="unknown_how_to_start",
            status=EmployeeInsightSessionStatus.BLOCKED,
            friction_points=["unknown_how_to_start"],
        )
    )
    await store.upsert_candidate(
        EmployeeInsightCandidate(
            candidate_id="cand_001",
            candidate_type=CandidateType.ONBOARDING,
            source_session_ids=["sess_002"],
            department_id="client",
            scenario_id="unknown_how_to_start",
            title="新手引导入口",
            summary="员工不知道怎么开始。",
            evidence=[{"session_id": "sess_002"}],
            confidence=0.86,
        )
    )

    snapshot = await EmployeeInsightDashboardSnapshot.from_store(store)

    assert snapshot.metrics["total_sessions"] == 2
    assert snapshot.metrics["completed_sessions"] == 1
    assert snapshot.metrics["blocked_sessions"] == 1
    assert snapshot.metrics["pending_candidates"] == 1
    top_scenarios = {
        item["scenario_id"]: item["count"] for item in snapshot.top_scenarios
    }
    assert top_scenarios["unknown_how_to_start"] == 1
    assert top_scenarios["write_notice"] == 1
    assert snapshot.friction_points[0]["friction_point"] == "unknown_how_to_start"


def test_hermes_policy_triggers_on_repeated_cross_employee_need() -> None:
    policy = HermesDeepDivePolicy(min_support_count=3)
    candidates = [
        EmployeeInsightCandidate(
            candidate_id=f"cand_{idx}",
            candidate_type=CandidateType.SKILL,
            source_session_ids=[f"sess_{idx}"],
            department_id="planning",
            scenario_id="generate_sop",
            title="需要 SOP skill",
            summary="多个员工需要把经验整理成 SOP。",
            evidence=[{"employee_hash": f"emp_{idx}"}],
            confidence=0.8,
        )
        for idx in range(3)
    ]

    tasks = policy.build_deep_dive_tasks(candidates)

    assert len(tasks) == 1
    assert tasks[0]["task_type"] == "employee_insight_deep_dive"
    assert tasks[0]["scenario_id"] == "generate_sop"
    assert tasks[0]["candidate_ids"] == ["cand_0", "cand_1", "cand_2"]
