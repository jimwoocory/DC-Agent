from __future__ import annotations

from dc_engines.department_workflows import (
    build_content_sop_workflow_request,
    match_department_workflow,
)


def _client_request(message: str):
    match = match_department_workflow(
        employee_department="客户部",
        text=message,
    )
    assert match is not None
    return build_content_sop_workflow_request(
        match,
        conversation_id="conv_channel",
        platform_id="巅池-Agent小助手",
        session_id="session_channel",
        source="test",
        message_text=message,
        content_type="copy",
    )


def test_client_content_defaults_to_private_domain_message_not_email() -> None:
    request = _client_request("帮我写端午节 VIP 客户问候话术")

    policy = request.payload["communication_channel_policy"]

    assert policy["default_channel"] == "wechat_private_domain_or_feishu_message"
    assert policy["should_use_email_format"] is False
    assert "客户邮件" in policy["blocked_defaults"]
    assert "不要问是否需要邮件审查" in policy["instruction"]


def test_client_email_format_is_allowed_only_when_explicit() -> None:
    request = _client_request("客户明确要求邮件，帮我写一封端午邮件")

    policy = request.payload["communication_channel_policy"]

    assert policy["explicit_email_requested"] is True
    assert policy["should_use_email_format"] is True


def test_client_workflow_training_directions_do_not_default_to_email() -> None:
    match = match_department_workflow(
        employee_department="客户部",
        text="端午 VIP 客户问候话术",
    )

    assert match is not None
    text = " ".join(match.scenario.test_task_directions)
    assert "问候邮件" not in text
    assert "问候话术" in text
