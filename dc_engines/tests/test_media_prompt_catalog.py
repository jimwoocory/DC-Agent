from __future__ import annotations

from dc_engines.media_prompt_catalog import (
    PROMPT_CATALOG_PRODUCT_POLICY,
    PROMPT_CATALOG_VERSION,
    PromptCatalogSelection,
    build_prompt_catalog_brief,
    build_prompt_catalog_context,
    get_prompt_catalog_atom,
    get_prompt_catalog_preset,
    list_prompt_catalog_atoms,
    list_prompt_catalog_presets,
)


def test_prompt_catalog_policy_is_user_reference_first() -> None:
    assert PROMPT_CATALOG_VERSION == "media_prompt_catalog.automotive_marketing.v1"
    assert PROMPT_CATALOG_PRODUCT_POLICY == (
        "user_reference_first",
        "low_friction_preset_first",
        "internal_proposal_after_feedback",
    )


def test_prompt_catalog_exposes_low_friction_presets() -> None:
    presets = list_prompt_catalog_presets()

    assert {preset.preset_id for preset in presets} >= {
        "automotive_wechat_cover",
        "automotive_xhs_cover",
        "automotive_festival_poster",
        "automotive_launch_key_visual",
    }
    assert get_prompt_catalog_preset(
        "automotive_wechat_cover"
    ).target_recipe_id == "image2_social_cover"


def test_prompt_catalog_atoms_are_grouped_by_slot_and_tag() -> None:
    lighting_atoms = list_prompt_catalog_atoms(slot="lighting")
    cover_atoms = list_prompt_catalog_atoms(tag="cover")
    atom = get_prompt_catalog_atom("camera.mobile_cover")

    assert len(lighting_atoms) >= 3
    assert len(cover_atoms) >= 2
    assert atom.slot == "camera"
    assert "mobile" in atom.tags


def test_prompt_catalog_context_compiles_selected_atoms() -> None:
    selection = PromptCatalogSelection(
        preset_id="automotive_xhs_cover",
        extra_atom_ids=("negative.no_text_noise",),
        custom_brief="标题要轻松，适合夏至节点",
    )

    context = build_prompt_catalog_context(selection, language="zh")

    assert "Prompt catalog preset: automotive_xhs_cover" in context
    assert "Target prompt recipe: image2_social_cover" in context
    assert "- subject:" in context
    assert "- scene:" in context
    assert "小红书封面风格" in context
    assert "排除乱码中文" in context
    assert "custom brief: 标题要轻松" in context


def test_prompt_catalog_brief_keeps_employee_brief_first() -> None:
    selection = PromptCatalogSelection(preset_id="automotive_wechat_cover")

    brief = build_prompt_catalog_brief("五菱缤果公众号封面", selection)

    assert brief.startswith("五菱缤果公众号封面")
    assert "Prompt catalog preset: automotive_wechat_cover" in brief
    assert "WeChat article cover style" in brief
