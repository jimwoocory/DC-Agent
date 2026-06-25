from __future__ import annotations

from dc_engines.media_prompt_catalog import PromptCatalogSelection
from dc_engines.media_sop import (
    MediaBrandVisualProfile,
    build_media_generation_record,
    build_structured_media_prompt,
    get_media_prompt_module,
    get_media_prompt_recipe,
    get_media_provider_prompt_compiler,
    list_media_prompt_modules,
    list_media_prompt_recipes,
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


def test_gpt_image2_prompt_compiles_marketing_visual_in_english() -> None:
    prompt = build_structured_media_prompt(
        "五菱缤果夏至海报，太阳、冰块、西瓜，标题「夏天一起出发」",
        media_kind="image",
        aspect_ratio="portrait",
        target_engine="gpt-image-2",
    )

    assert "Provider: GPT Image 2 primary" in prompt
    assert "Prompt library: media_sop.image_marketing_visual.v1" in prompt
    assert "Prompt recipe: image2_premium_key_visual" in prompt
    assert "Prompt modules: image2.source_brief_lock" in prompt
    assert "image2.brand_asset_series" in prompt
    assert "image2.vehicle_model_accuracy" in prompt
    assert "image2.brand_vi_system" in prompt
    assert "premium Chinese automotive marketing key visual" in prompt
    assert "preserve their Chinese characters exactly" in prompt
    assert "Use provided brand references" in prompt
    assert "Do not mix different generations" in prompt
    assert "consistent brand VI system" in prompt
    assert "五菱缤果夏至海报" in prompt
    assert "Negative visual constraints:" in prompt
    assert "cheap sticker-like sun" in prompt


def test_dreamina_prompt_keeps_chinese_marketing_visual_direction() -> None:
    prompt = build_structured_media_prompt(
        "五菱缤果夏至海报",
        media_kind="image",
        aspect_ratio="portrait",
        target_engine="dreamina",
    )

    assert "Provider: Dreamina fallback or explicit opt-in" in prompt
    assert "Prompt library: media_sop.image_marketing_visual.v1" in prompt
    assert "Prompt recipe: dreamina_chinese_marketing_default" in prompt
    assert "Prompt modules: dreamina.chinese_social_poster" in prompt
    assert "dreamina.brand_asset_series" in prompt
    assert "中文营销视觉要求:" in prompt
    assert "品牌素材与车型约束:" in prompt
    assert "画面质感要求:" in prompt
    assert "中文社媒海报质感" in prompt
    assert "画幅: portrait" in prompt


def test_image_prompt_accepts_source_grounded_brand_visual_profile() -> None:
    profile = MediaBrandVisualProfile(
        brand_name="五菱",
        vehicle_series="缤果",
        model_name="缤果 PLUS",
        colors=("奶咖白", "极光绿"),
        trims=("2026 款",),
        vi_colors=("#E32222", "#FFFFFF"),
        typography="中文标题使用清晰黑体，避免艺术字变形",
        logo_usage="只使用已提供的官方 logo 素材",
        visual_effects=("夏日阳光", "真实冰块", "清爽水汽"),
        asset_references=("obsidian://brand/wuling/bingo-plus",),
        missing_assets=("官方 logo 原文件",),
        source_note="来自品牌素材库摘要",
    )

    image2_prompt = build_structured_media_prompt(
        "五菱缤果夏至海报",
        media_kind="image",
        aspect_ratio="portrait",
        target_engine="gpt-image-2",
        brand_profile=profile,
    )
    dreamina_prompt = build_structured_media_prompt(
        "五菱缤果夏至海报",
        media_kind="image",
        aspect_ratio="portrait",
        target_engine="dreamina",
        brand_profile=profile,
    )

    assert "Brand visual profile:" in image2_prompt
    assert "brand=五菱" in image2_prompt
    assert "vehicle series=缤果" in image2_prompt
    assert "asset references=obsidian://brand/wuling/bingo-plus" in image2_prompt
    assert "missing assets=官方 logo 原文件" in image2_prompt
    assert "do not invent missing brand assets" in image2_prompt
    assert "品牌视觉 Profile:" in dreamina_prompt
    assert "品牌=五菱" in dreamina_prompt
    assert "车系=缤果" in dreamina_prompt
    assert "素材来源=obsidian://brand/wuling/bingo-plus" in dreamina_prompt
    assert "缺失素材=官方 logo 原文件" in dreamina_prompt


def test_media_prompt_module_library_exposes_provider_modules() -> None:
    image2_modules = list_media_prompt_modules(
        channel="image_marketing_visual",
        target_engine="gpt-image-2",
    )
    dreamina_modules = list_media_prompt_modules(
        channel="image_marketing_visual",
        target_engine="dreamina",
    )
    source_lock = get_media_prompt_module("image2.source_brief_lock")
    brand_assets = get_media_prompt_module("image2.brand_asset_series")
    vehicle_accuracy = get_media_prompt_module("image2.vehicle_model_accuracy")
    dreamina_assets = get_media_prompt_module("dreamina.brand_asset_series")

    assert len(image2_modules) >= 12
    assert len(dreamina_modules) >= 5
    assert source_lock.language == "en"
    assert source_lock.label == "Model Prompt"
    assert brand_assets.label == "Brand asset handling"
    assert "VI colors" in brand_assets.content
    assert "trim" in vehicle_accuracy.content
    assert dreamina_assets.language == "zh"


def test_media_prompt_recipe_library_exposes_composable_module_sets() -> None:
    recipes = list_media_prompt_recipes(
        channel="image_marketing_visual",
        target_engine="gpt-image-2",
    )
    social_recipe = get_media_prompt_recipe("image2_social_cover")

    assert {recipe.recipe_id for recipe in recipes} >= {
        "image2_premium_key_visual",
        "image2_social_cover",
        "image2_festival_campaign",
    }
    assert social_recipe.target_engine == "gpt-image-2"
    assert "image2.social_platform_finish" in social_recipe.module_ids
    assert "image2.campaign_background" not in social_recipe.module_ids


def test_image_prompt_can_select_a_specific_prompt_recipe() -> None:
    prompt = build_structured_media_prompt(
        "五菱缤果小红书封面",
        media_kind="image",
        aspect_ratio="portrait",
        target_engine="gpt-image-2",
        recipe_id="image2_social_cover",
    )

    assert "Prompt recipe: image2_social_cover" in prompt
    assert "image2.social_platform_finish" in prompt
    assert "image2.campaign_background" not in prompt
    assert "Social platform finish:" in prompt


def test_image_prompt_rejects_provider_mismatched_recipe() -> None:
    try:
        build_structured_media_prompt(
            "五菱缤果海报",
            media_kind="image",
            target_engine="gpt-image-2",
            recipe_id="dreamina_chinese_marketing_default",
        )
    except ValueError as exc:
        assert "not gpt-image-2" in str(exc)
    else:
        raise AssertionError("provider-mismatched recipe should fail")


def test_image_prompt_accepts_low_friction_prompt_catalog_selection() -> None:
    prompt = build_structured_media_prompt(
        "五菱缤果夏至小红书封面",
        media_kind="image",
        aspect_ratio="portrait",
        target_engine="gpt-image-2",
        catalog_selection=PromptCatalogSelection(
            preset_id="automotive_xhs_cover",
            extra_atom_ids=("negative.no_text_noise",),
        ),
    )

    assert "Prompt recipe: image2_social_cover" in prompt
    assert "Prompt catalog preset: automotive_xhs_cover" in prompt
    assert "Target prompt recipe: image2_social_cover" in prompt
    assert "Rednote cover style" in prompt
    assert "Avoid garbled Chinese text" in prompt
    assert "image2.campaign_background" not in prompt


def test_image_prompt_compiler_registry_keeps_image2_primary_with_dreamina_fallback() -> None:
    image2_compiler = get_media_provider_prompt_compiler("gpt-image-2")
    dreamina_compiler = get_media_provider_prompt_compiler("dreamina")

    assert image2_compiler.channel == "image_marketing_visual"
    assert image2_compiler.provider_role == "primary image generation compiler"
    assert image2_compiler.fallback_target == "dreamina"
    assert dreamina_compiler.channel == "image_marketing_visual"
    assert dreamina_compiler.provider_role == "fallback or explicit image generation compiler"


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
