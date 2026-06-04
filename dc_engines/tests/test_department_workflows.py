from __future__ import annotations

from dc_engines.department_workflows import (
    DEFAULT_DEPARTMENT_WORKFLOWS,
    assess_material_intake,
    build_content_sop_workflow_request,
    build_department_workflow_request,
    match_department_workflow,
    workflow_catalog,
)


def test_default_registry_covers_required_departments() -> None:
    names = {workflow.department_name for workflow in DEFAULT_DEPARTMENT_WORKFLOWS}

    assert names == {
        "总经办",
        "客户部",
        "策划",
        "品宣部",
        "执行运营",
        "综合部",
        "财务部",
    }


def test_department_and_text_match_best_scenario() -> None:
    match = match_department_workflow(
        employee_department="市场部",
        text="帮我做一份端午营销活动方案，包含渠道规划和 KPI",
    )

    assert match is not None
    assert match.department_id == "client_dept"
    assert match.scenario_id == "campaign_plan"
    assert "department_alias" in match.reasons
    assert "scenario_keyword" in match.reasons


def test_department_only_does_not_match_by_default() -> None:
    match = match_department_workflow(
        employee_department="市场部",
        text="你好，今天辛苦了",
    )

    assert match is None


def test_text_can_match_department_scenario_without_employee_profile() -> None:
    match = match_department_workflow(
        text="帮我整理一份报销发票材料检查清单和缺失材料提醒",
    )

    assert match is not None
    assert match.department_id == "finance"
    assert match.scenario_id == "reimbursement_invoice_check"


def test_finance_department_matches_budget_summary() -> None:
    match = match_department_workflow(
        employee_department="财务",
        text="帮我把本月费用明细做一个预算执行差异汇总",
    )

    assert match is not None
    assert match.department_id == "finance"
    assert match.scenario_id == "budget_cost_summary"


def test_legacy_department_ids_resolve_to_current_workflows() -> None:
    catalog = workflow_catalog()
    ids = {item["department_id"] for item in catalog}

    assert "client_dept" in ids
    assert "planning" in ids
    assert "general_affairs" in ids

    match = match_department_workflow(
        employee_department="strategy",
        text="帮我做一份竞品洞察和行动建议",
    )

    assert match is not None
    assert match.department_id == "planning"


def test_build_request_payload_contains_harness_requirements() -> None:
    match = match_department_workflow(
        employee_department="总经办",
        text="把昨天会议要点整理成正式会议纪要，列出决策和行动项",
    )

    assert match is not None
    request = build_department_workflow_request(
        match,
        conversation_id="conv_1",
        platform_id="巅池-Agent小助手",
        session_id="lark:chat_1",
        source="test",
        message_text="把昨天会议要点整理成正式会议纪要，列出决策和行动项",
        requester_meta={
            "requester_open_id": "ou_admin",
            "requester_department": "总经办",
        },
    )

    assert request.domain == "department_workflow:executive_office"
    assert request.title.startswith("部门工作流 | 总经办 | 会议纪要整理")
    assert request.payload["workflow_kind"] == "department_workflow"
    assert request.payload["department_id"] == "executive_office"
    assert request.payload["scenario_id"] == "meeting_minutes"
    assert request.payload["requester_open_id"] == "ou_admin"
    assert request.payload["required_inputs"][0]["key"] == "meeting_topic"
    assert {item["key"] for item in request.payload["expected_outputs"]} >= {
        "summary",
        "decisions",
        "action_items",
    }
    assert request.payload["truth_requirements"]
    assert request.payload["material_requirements"]


def test_material_assessment_marks_missing_inputs_without_materials() -> None:
    match = match_department_workflow(
        employee_department="市场部",
        text="帮我做一份端午营销活动方案",
    )

    assert match is not None
    assessment = assess_material_intake(match.scenario, "帮我做一份端午营销活动方案")

    assert assessment.status == "needs_materials"
    assert assessment.needs_followup is True
    assert {item.key for item in assessment.missing_required_inputs} >= {
        "brand_or_product",
        "goal",
        "audience",
    }


def test_request_payload_records_material_loop_state() -> None:
    match = match_department_workflow(
        employee_department="财务",
        text="这是发票截图 [image]，帮我整理报销材料检查清单",
    )

    assert match is not None
    request = build_department_workflow_request(
        match,
        conversation_id="conv_2",
        platform_id="巅池-Agent小助手",
        session_id="lark:chat_2",
        source="test",
        message_text="这是截图 [image]，帮我整理短视频拍摄脚本结构",
    )

    assert request.payload["material_status"] in {"partial", "ready"}
    assert request.payload["truth_status"] in {
        "needs_materials",
        "ready_for_execution",
    }
    assert request.payload["next_actions"]


def test_workflow_catalog_is_json_ready() -> None:
    catalog = workflow_catalog()

    assert len(catalog) == 7
    client = next(item for item in catalog if item["department_id"] == "client_dept")
    assert client["department_name"] == "客户部"
    assert client["scenarios"][0]["required_inputs"][0]["key"]


def test_client_content_package_matches_copy_image_video_request() -> None:
    match = match_department_workflow(
        employee_department="客户部",
        text="帮我写客户邀约文案并配图，再给一个短视频脚本",
    )

    assert match is not None
    assert match.department_id == "client_dept"
    assert match.scenario_id == "client_content_package"


def test_client_p0_scenarios_match_specific_workflows() -> None:
    cases = [
        ("客户部", "沉默客户复联，目标是促进复购，请给我回访话术", "client_followup"),
        ("客户部", "端午试驾活动邀约，邀请客户报名到店", "event_invitation"),
        ("市场部", "做一套朋友圈和微信群的私域活动转化话术", "private_domain_campaign"),
    ]

    for department, text, expected_scenario in cases:
        match = match_department_workflow(employee_department=department, text=text)

        assert match is not None
        assert match.department_id == "client_dept"
        assert match.scenario_id == expected_scenario


def test_client_followup_material_gap_blocks_generation() -> None:
    match = match_department_workflow(
        employee_department="客户部",
        text="帮我写客户回访话术",
    )

    assert match is not None
    assert match.scenario_id == "client_followup"
    request = build_content_sop_workflow_request(
        match,
        conversation_id="conv_client_followup",
        platform_id="巅池-Agent小助手",
        session_id="lark:chat_client_followup",
        source="test",
        message_text="帮我写客户回访话术",
        content_type="copy",
    )

    missing_keys = {item["key"] for item in request.payload["missing_required_inputs"]}
    assert request.payload["lifecycle_stage"] == "needs_materials"
    assert request.payload["generation_allowed"] is False
    assert missing_keys >= {"client_segment", "last_interaction", "followup_goal"}
    assert (
        request.payload["communication_channel_policy"]["should_use_email_format"]
        is False
    )


def test_client_private_domain_ready_payload_has_review_outputs() -> None:
    text = (
        "人群分层：老客户和高意向客户；活动机制：端午到店预约礼；"
        "私域渠道：朋友圈和微信群；转化目标：预约到店；"
        "帮我做私域活动转化话术"
    )
    match = match_department_workflow(employee_department="客户部", text=text)

    assert match is not None
    assert match.scenario_id == "private_domain_campaign"
    request = build_content_sop_workflow_request(
        match,
        conversation_id="conv_private_domain",
        platform_id="巅池-Agent小助手",
        session_id="lark:chat_private_domain",
        source="test",
        message_text=text,
        content_type="copy",
        knowledge_context="来源路径: campaigns/端午私域活动.md\n端午到店预约礼规则。",
        source_citations=[
            {
                "title": "端午私域活动规则",
                "source_path": "campaigns/端午私域活动.md",
            }
        ],
    )

    output_keys = {item["key"] for item in request.payload["expected_outputs"]}
    assert request.payload["lifecycle_stage"] == "ready_for_generation"
    assert request.payload["generation_allowed"] is True
    assert output_keys >= {
        "segment_strategy",
        "message_variants",
        "posting_sequence",
        "review_checklist",
    }


def test_planning_video_visual_brief_matches_mixed_request() -> None:
    match = match_department_workflow(
        employee_department="策划部",
        text="做一个短视频脚本和生图 prompt，要有分镜和旁白",
    )

    assert match is not None
    assert match.department_id == "planning"
    assert match.scenario_id == "short_video_visual_brief"


def test_planning_p0_scenarios_match_storyboard_and_brand_review() -> None:
    cases = [
        (
            "策划部",
            "帮我做之光EV短视频分镜脚本，要有镜头表、转场和旁白字幕",
            "video_storyboard_production",
        ),
        (
            "策划部",
            "帮我做品牌审查和禁用词检查，看看这版视频脚本有没有口径风险",
            "brand_review_check",
        ),
    ]

    for department, text, expected_scenario in cases:
        match = match_department_workflow(employee_department=department, text=text)

        assert match is not None
        assert match.department_id == "planning"
        assert match.scenario_id == expected_scenario


def test_planning_brand_review_requires_material_and_guidelines() -> None:
    match = match_department_workflow(
        employee_department="策划部",
        text="帮我做品牌审查",
    )

    assert match is not None
    assert match.scenario_id == "brand_review_check"
    request = build_content_sop_workflow_request(
        match,
        conversation_id="conv_brand_review",
        platform_id="巅池-Agent小助手",
        session_id="lark:chat_brand_review",
        source="test",
        message_text="帮我做品牌审查",
        content_type="copy",
    )

    missing_keys = {item["key"] for item in request.payload["missing_required_inputs"]}
    assert request.payload["lifecycle_stage"] == "needs_materials"
    assert request.payload["generation_allowed"] is False
    assert missing_keys >= {"review_material", "brand_guideline", "usage_context"}


def test_content_sop_payload_blocks_generation_when_materials_are_missing() -> None:
    match = match_department_workflow(
        employee_department="客户部",
        text="帮我写客户邀约文案并配图",
    )

    assert match is not None
    request = build_content_sop_workflow_request(
        match,
        conversation_id="conv_content_1",
        platform_id="巅池-Agent小助手",
        session_id="lark:chat_content_1",
        source="test",
        message_text="帮我写客户邀约文案并配图",
        content_type="mixed",
    )

    assert request.domain == "content_sop:client_dept"
    assert request.payload["workflow_kind"] == "content_sop_workflow"
    assert request.payload["lifecycle_stage"] == "needs_materials"
    assert request.payload["generation_allowed"] is False
    assert request.payload["missing_required_inputs"]
    assert "send_material_intake_card" in request.payload["next_actions"]
    assert "pause_before_generation" in request.payload["next_actions"]


def test_content_sop_payload_contains_full_media_requirements() -> None:
    match = match_department_workflow(
        employee_department="策划部",
        text=(
            "项目/品牌：之光EV；传播目标：活动预热；目标人群：年轻家庭；"
            "发布平台：视频号；帮我做短视频脚本和生图 prompt"
        ),
    )

    assert match is not None
    request = build_content_sop_workflow_request(
        match,
        conversation_id="conv_content_2",
        platform_id="巅池-Agent小助手",
        session_id="lark:chat_content_2",
        source="test",
        message_text=(
            "项目/品牌：之光EV；传播目标：活动预热；目标人群：年轻家庭；"
            "发布平台：视频号；帮我做短视频脚本和生图 prompt"
        ),
        content_type="mixed",
        knowledge_context="来源路径: projects/之光EV/传播策略.md\n已有活动预热资料。",
        source_citations=[
            {
                "title": "之光EV传播策略",
                "source_path": "projects/之光EV/传播策略.md",
            }
        ],
    )

    output_keys = {item["key"] for item in request.payload["expected_outputs"]}
    assert request.payload["lifecycle_stage"] == "ready_for_generation"
    assert request.payload["generation_allowed"] is True
    assert request.payload["knowledge_context"]
    assert request.payload["source_citations"][0]["source_path"].endswith("传播策略.md")
    assert output_keys >= {
        "creative_brief",
        "video_script",
        "storyboard",
        "voiceover",
        "image_prompt",
        "source_citations",
        "review_checklist",
    }
