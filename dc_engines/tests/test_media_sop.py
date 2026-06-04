from __future__ import annotations

from dc_engines.media_sop import (
    build_media_generation_record,
    build_structured_media_prompt,
)


def test_image_prompt_is_wrapped_as_content_sop_spec() -> None:
    prompt = build_structured_media_prompt(
        "端午客户邀约海报，温暖家庭氛围",
        media_kind="image",
        aspect_ratio="portrait",
    )

    assert "业务说明: 端午客户邀约海报" in prompt
    assert "模型 Prompt: 端午客户邀约海报" in prompt
    assert "画幅: portrait" in prompt
    assert "禁用元素:" in prompt


def test_video_prompt_is_wrapped_with_storyboard_guardrails() -> None:
    prompt = build_structured_media_prompt(
        "策划一个 15 秒活动预热视频",
        media_kind="video",
    )

    assert "业务说明: 策划一个 15 秒活动预热视频" in prompt
    assert "模型 Prompt: 策划一个 15 秒活动预热视频" in prompt
    assert "分镜:" in prompt
    assert "旁白/字幕:" in prompt


def test_structured_media_prompt_is_idempotent() -> None:
    prompt = "业务说明: 已审核 brief\n模型 Prompt: 已审核模型 prompt"

    assert build_structured_media_prompt(prompt, media_kind="image") == prompt


def test_media_generation_record_contains_rollback_hint() -> None:
    record = build_media_generation_record(
        media_kind="image",
        prompt="端午客户邀约海报",
        engine="GPT Image 2",
        status="succeeded",
        aspect_ratio="portrait",
        output_path="/tmp/demo.png",
    )

    assert record.record_id.startswith("media_")
    assert record.status == "succeeded"
    assert "业务说明: 端午客户邀约海报" in record.structured_prompt
    assert "回滚" in record.rollback_hint
    assert record.to_dict()["output_path"] == "/tmp/demo.png"
    assert record.record_id in record.to_card_detail()
