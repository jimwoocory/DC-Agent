from __future__ import annotations

from dc_engines.department_workflows import (
    build_content_sop_workflow_request,
    evaluate_content_sop_payload,
    match_department_workflow,
)


def _client_payload(message: str, *, source_citations=None):
    match = match_department_workflow(
        employee_department="客户部",
        text=message,
    )
    assert match is not None
    request = build_content_sop_workflow_request(
        match,
        conversation_id="conv_quality",
        platform_id="巅池-Agent小助手",
        session_id="session_quality",
        source="test",
        message_text=message,
        content_type="mixed",
        source_citations=source_citations or [],
    )
    return request.payload


def test_quality_gate_blocks_missing_materials_before_generation() -> None:
    payload = _client_payload("帮我写客户邀约文案并配图")

    quality = evaluate_content_sop_payload(payload)

    assert quality.status == "blocked"
    assert quality.can_deliver is False
    assert any("资料不足" in reason for reason in quality.blocked_reasons)
    assert payload["quality_gate"]["status"] == "blocked"


def test_quality_gate_passes_source_backed_complete_result() -> None:
    payload = _client_payload(
        "客户/受众：VIP车主；触达场景：端午私域触达；品牌/产品：之光EV；业务目标：邀约到店；帮我做客户触达内容包，包含客户文案、配图、生图和视频脚本",
        source_citations=[
            {
                "title": "之光EV传播策略",
                "source_path": "projects/之光EV/传播策略.md",
            }
        ],
    )

    quality = evaluate_content_sop_payload(
        payload,
        {
            "message_draft": "VIP 试驾邀约文案",
            "image_prompt": "业务说明: 之光EV邀约海报\n模型 Prompt: 温暖家庭场景",
            "video_script": "三镜头试驾邀约脚本",
            "storyboard": "镜头 1-3",
            "source_citations": [{"source_path": "projects/之光EV/传播策略.md"}],
            "review_checklist": ["核对权益", "核对品牌口径"],
        },
    )

    assert quality.status == "review_required"
    assert quality.blocked_reasons == ()
    assert quality.score >= 80


def test_quality_gate_blocks_fact_sensitive_result_without_sources() -> None:
    payload = _client_payload(
        "客户/受众：VIP车主；触达场景：端午私域触达；品牌/产品：之光EV；业务目标：邀约到店；帮我做客户触达内容包，包含客户文案、配图、生图和视频脚本"
    )

    quality = evaluate_content_sop_payload(
        payload,
        {
            "message_draft": "VIP 试驾邀约文案",
            "image_prompt": "模型 Prompt",
            "video_script": "脚本",
            "storyboard": "分镜",
            "review_checklist": ["核对权益"],
        },
    )

    assert quality.status == "blocked"
    assert any("来源依据" in reason for reason in quality.blocked_reasons)


def test_quality_gate_blocks_customer_email_format_without_explicit_request() -> None:
    payload = _client_payload(
        "客户/受众：VIP车主；触达场景：试驾邀约；品牌/产品：之光EV；业务目标：邀约到店；帮我写客户问候话术",
        source_citations=[
            {
                "title": "之光EV传播策略",
                "source_path": "projects/之光EV/传播策略.md",
            }
        ],
    )

    quality = evaluate_content_sop_payload(
        payload,
        {
            "message_draft": "邮件主题：端午安康\n邮件正文：尊敬的客户，您好……",
            "image_prompt": "模型提示词",
            "video_script": "脚本",
            "storyboard": "分镜",
            "source_citations": [{"source_path": "projects/之光EV/传播策略.md"}],
            "review_checklist": ["核对权益"],
        },
    )

    assert quality.status == "blocked"
    assert any("禁止输出邮件格式" in reason for reason in quality.blocked_reasons)
