"""Shared SOP prompt helpers for image and video generation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Literal

from dc_engines.media_prompt_catalog import (
    PromptCatalogSelection,
    build_prompt_catalog_brief,
    get_prompt_catalog_preset,
)

MediaKind = Literal["image", "video", "image2video"]
MediaGenerationStatus = Literal["running", "succeeded", "failed"]
MediaPromptTarget = Literal["generic", "gpt-image-2", "dreamina"]
MediaPromptChannel = Literal["generic_media", "image_marketing_visual", "video_storyboard"]
MediaPromptLanguage = Literal["en", "zh"]
MediaPromptRecipeId = Literal[
    "image2_premium_key_visual",
    "image2_social_cover",
    "image2_festival_campaign",
    "dreamina_chinese_marketing_default",
]


@dataclass(frozen=True, slots=True)
class MediaProviderPromptCompiler:
    target_engine: MediaPromptTarget
    channel: MediaPromptChannel
    display_name: str
    provider_role: str
    fallback_target: MediaPromptTarget | None = None


@dataclass(frozen=True, slots=True)
class MediaPromptModule:
    module_id: str
    channel: MediaPromptChannel
    provider_targets: tuple[MediaPromptTarget, ...]
    label: str
    language: MediaPromptLanguage
    content: str


@dataclass(frozen=True, slots=True)
class MediaPromptRecipe:
    recipe_id: MediaPromptRecipeId
    channel: MediaPromptChannel
    target_engine: MediaPromptTarget
    display_name: str
    module_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MediaBrandVisualProfile:
    brand_name: str = ""
    vehicle_series: str = ""
    model_name: str = ""
    colors: tuple[str, ...] = ()
    trims: tuple[str, ...] = ()
    vi_colors: tuple[str, ...] = ()
    typography: str = ""
    logo_usage: str = ""
    visual_effects: tuple[str, ...] = ()
    asset_references: tuple[str, ...] = ()
    missing_assets: tuple[str, ...] = ()
    source_note: str = ""

    @property
    def has_content(self) -> bool:
        return any(
            (
                self.brand_name,
                self.vehicle_series,
                self.model_name,
                self.colors,
                self.trims,
                self.vi_colors,
                self.typography,
                self.logo_usage,
                self.visual_effects,
                self.asset_references,
                self.missing_assets,
                self.source_note,
            )
        )


MEDIA_PROVIDER_PROMPT_COMPILERS: dict[MediaPromptTarget, MediaProviderPromptCompiler] = {
    "generic": MediaProviderPromptCompiler(
        target_engine="generic",
        channel="generic_media",
        display_name="Generic media SOP",
        provider_role="shared fallback compiler",
    ),
    "gpt-image-2": MediaProviderPromptCompiler(
        target_engine="gpt-image-2",
        channel="image_marketing_visual",
        display_name="GPT Image 2 marketing visual compiler",
        provider_role="primary image generation compiler",
        fallback_target="dreamina",
    ),
    "dreamina": MediaProviderPromptCompiler(
        target_engine="dreamina",
        channel="image_marketing_visual",
        display_name="Dreamina Chinese marketing visual compiler",
        provider_role="fallback or explicit image generation compiler",
    ),
}

MEDIA_PROMPT_LIBRARY_VERSION = "media_sop.image_marketing_visual.v1"

MediaPromptInstructionModule = MediaPromptModule

MEDIA_PROMPT_MODULE_LIBRARY: dict[str, MediaPromptModule] = {
    "image2.source_brief_lock": MediaPromptModule(
        module_id="image2.source_brief_lock",
        channel="image_marketing_visual",
        provider_targets=("gpt-image-2",),
        label="Model Prompt",
        language="en",
        content=(
            "Create a premium Chinese automotive marketing key visual based on "
            "the source brief below. Treat the Chinese brand names, vehicle model "
            "names, slogans, and headline text as locked text tokens; preserve "
            "their Chinese characters exactly when they appear in the image. Build "
            "a polished social-media poster or cover visual with commercial "
            "automotive advertising quality."
        ),
    ),
    "image2.automotive_hero_visual": MediaPromptModule(
        module_id="image2.automotive_hero_visual",
        channel="image_marketing_visual",
        provider_targets=("gpt-image-2",),
        label="Visual direction",
        language="en",
        content=(
            "Premium automotive campaign, clean hero composition, credible car "
            "lighting, refined reflections, controlled contrast, layered depth, "
            "and a modern Chinese social-media poster aesthetic. The vehicle or "
            "product subject should be the clear first visual focus."
        ),
    ),
    "image2.brand_asset_series": MediaPromptModule(
        module_id="image2.brand_asset_series",
        channel="image_marketing_visual",
        provider_targets=("gpt-image-2",),
        label="Brand asset handling",
        language="en",
        content=(
            "Use provided brand references, product images, logo files, VI colors, "
            "campaign key visuals, and approved slogans as the highest-priority "
            "visual source. If the brief names a brand or vehicle series but does "
            "not provide official assets, infer only broad category cues and avoid "
            "inventing exact logos, badges, legal marks, special editions, or "
            "official campaign claims."
        ),
    ),
    "image2.vehicle_model_accuracy": MediaPromptModule(
        module_id="image2.vehicle_model_accuracy",
        channel="image_marketing_visual",
        provider_targets=("gpt-image-2",),
        label="Vehicle and product accuracy",
        language="en",
        content=(
            "When a vehicle model, trim, color, wheel style, body shape, or product "
            "variant appears in the brief, keep the visible subject consistent with "
            "that description. Do not mix different generations, body styles, brand "
            "families, or unrelated concept-car features. Keep wheels, windows, "
            "lights, proportions, badges, and reflections physically coherent."
        ),
    ),
    "image2.color_trim_materials": MediaPromptModule(
        module_id="image2.color_trim_materials",
        channel="image_marketing_visual",
        provider_targets=("gpt-image-2",),
        label="Color, trim, and material finish",
        language="en",
        content=(
            "Respect requested vehicle colors, product colors, seasonal palettes, "
            "materials, finishes, and trim details. Make paint, glass, chrome, "
            "fabric, leather, fruit, ice, water, sunlight, and shadows look "
            "photographically plausible rather than plastic, flat, or pasted on."
        ),
    ),
    "image2.brand_vi_system": MediaPromptModule(
        module_id="image2.brand_vi_system",
        channel="image_marketing_visual",
        provider_targets=("gpt-image-2",),
        label="Brand VI system",
        language="en",
        content=(
            "Create a consistent brand VI system for the poster: controlled color "
            "palette, aligned typography zones, disciplined spacing, restrained "
            "decorative elements, and a hierarchy suitable for Chinese social "
            "media. If exact VI rules are not supplied, keep the design premium, "
            "clean, and campaign-ready instead of guessing proprietary layouts."
        ),
    ),
    "image2.social_platform_finish": MediaPromptModule(
        module_id="image2.social_platform_finish",
        channel="image_marketing_visual",
        provider_targets=("gpt-image-2",),
        label="Social platform finish",
        language="en",
        content=(
            "Finish the visual for practical Chinese marketing use: readable on "
            "mobile, strong cover-thumbnail impact, clear headline-safe area, "
            "clean crop margins, and enough negative space for WeChat, Rednote, "
            "Douyin, or video cover adaptation."
        ),
    ),
    "image2.commercial_composition": MediaPromptModule(
        module_id="image2.commercial_composition",
        channel="image_marketing_visual",
        provider_targets=("gpt-image-2",),
        label="Composition",
        language="en",
        content=(
            "Use a deliberate art-directed layout: hero subject in the visual "
            "center or lower third, balanced negative space for Chinese headline "
            "copy, stable horizon, no clutter, no random floating decorations. "
            "For poster and mobile cover work, favor portrait-first framing."
        ),
    ),
    "image2.campaign_background": MediaPromptModule(
        module_id="image2.campaign_background",
        channel="image_marketing_visual",
        provider_targets=("gpt-image-2",),
        label="Background and atmosphere",
        language="en",
        content=(
            "Design the environment as a premium campaign background, not a "
            "generic clip-art scene. Seasonal elements such as sun, sky, fruit, "
            "ice, water, or festival props must look realistic, elegant, and "
            "integrated with the lighting direction."
        ),
    ),
    "image2.poster_typography": MediaPromptModule(
        module_id="image2.poster_typography",
        channel="image_marketing_visual",
        provider_targets=("gpt-image-2",),
        label="Typography",
        language="en",
        content=(
            "If Chinese text is rendered, keep it minimal, legible, and "
            "poster-grade. Avoid distorted Chinese characters. Do not invent "
            "extra claims, prices, awards, official endorsements, or legal copy."
        ),
    ),
    "image2.low_quality_negative": MediaPromptModule(
        module_id="image2.low_quality_negative",
        channel="image_marketing_visual",
        provider_targets=("gpt-image-2",),
        label="Negative visual constraints",
        language="en",
        content=(
            "Avoid low-end stock poster style, childish illustration, plastic "
            "rendering, messy gradients, cheap sticker-like sun or background "
            "elements, random logos, warped cars, broken wheels, unreadable "
            "Chinese text, and overdecorated layouts."
        ),
    ),
    "image2.brand_safety": MediaPromptModule(
        module_id="image2.brand_safety",
        channel="image_marketing_visual",
        provider_targets=("gpt-image-2",),
        label="Brand safety",
        language="en",
        content=(
            "Follow the provided brand wording only. If exact brand assets are "
            "not supplied, do not fabricate detailed legal marks, prices, "
            "benefits, customer promises, or official certification badges."
        ),
    ),
    "dreamina.chinese_social_poster": MediaPromptModule(
        module_id="dreamina.chinese_social_poster",
        channel="image_marketing_visual",
        provider_targets=("dreamina",),
        label="中文营销视觉要求",
        language="zh",
        content=(
            "中文社媒海报质感，主体突出，构图干净，适合公众号封面、朋友圈海报、"
            "小红书/视频号封面；中文标题尽量少而准，避免花哨廉价背景。"
        ),
    ),
    "dreamina.brand_asset_series": MediaPromptModule(
        module_id="dreamina.brand_asset_series",
        channel="image_marketing_visual",
        provider_targets=("dreamina",),
        label="品牌素材与车型约束",
        language="zh",
        content=(
            "优先遵守输入里的品牌、车型、颜色、款式、Logo、VI 色和活动主视觉；"
            "没有给到官方素材时，只能做宽泛风格参考，不要臆造官方标识、"
            "特殊版型、权益口径或认证徽章。"
        ),
    ),
    "dreamina.visual_finish": MediaPromptModule(
        module_id="dreamina.visual_finish",
        channel="image_marketing_visual",
        provider_targets=("dreamina",),
        label="画面质感要求",
        language="zh",
        content=(
            "车辆比例、车灯、轮毂、玻璃、反光、背景和季节元素要自然统一；"
            "避免塑料感、贴纸感、廉价渐变、杂乱装饰和粗糙背景。"
        ),
    ),
    "dreamina.brand_guardrails": MediaPromptModule(
        module_id="dreamina.brand_guardrails",
        channel="image_marketing_visual",
        provider_targets=("dreamina",),
        label="品牌约束",
        language="zh",
        content="遵守品牌口径；如缺少品牌素材，不要虚构具体 logo、权益或价格。",
    ),
    "dreamina.forbidden_elements": MediaPromptModule(
        module_id="dreamina.forbidden_elements",
        channel="image_marketing_visual",
        provider_targets=("dreamina",),
        label="禁用元素",
        language="zh",
        content="不得生成未经提供的客户承诺、价格优惠、官方背书或真实个人隐私。",
    ),
}

MEDIA_PROMPT_INSTRUCTION_LIBRARY = MEDIA_PROMPT_MODULE_LIBRARY

MEDIA_PROMPT_RECIPE_LIBRARY: dict[MediaPromptRecipeId, MediaPromptRecipe] = {
    "image2_premium_key_visual": MediaPromptRecipe(
        recipe_id="image2_premium_key_visual",
        channel="image_marketing_visual",
        target_engine="gpt-image-2",
        display_name="GPT Image 2 premium automotive key visual",
        module_ids=(
            "image2.source_brief_lock",
            "image2.automotive_hero_visual",
            "image2.brand_asset_series",
            "image2.vehicle_model_accuracy",
            "image2.color_trim_materials",
            "image2.brand_vi_system",
            "image2.social_platform_finish",
            "image2.commercial_composition",
            "image2.campaign_background",
            "image2.poster_typography",
            "image2.low_quality_negative",
            "image2.brand_safety",
        ),
    ),
    "image2_social_cover": MediaPromptRecipe(
        recipe_id="image2_social_cover",
        channel="image_marketing_visual",
        target_engine="gpt-image-2",
        display_name="GPT Image 2 mobile social cover",
        module_ids=(
            "image2.source_brief_lock",
            "image2.brand_asset_series",
            "image2.vehicle_model_accuracy",
            "image2.brand_vi_system",
            "image2.social_platform_finish",
            "image2.commercial_composition",
            "image2.poster_typography",
            "image2.low_quality_negative",
            "image2.brand_safety",
        ),
    ),
    "image2_festival_campaign": MediaPromptRecipe(
        recipe_id="image2_festival_campaign",
        channel="image_marketing_visual",
        target_engine="gpt-image-2",
        display_name="GPT Image 2 seasonal campaign poster",
        module_ids=(
            "image2.source_brief_lock",
            "image2.automotive_hero_visual",
            "image2.brand_asset_series",
            "image2.vehicle_model_accuracy",
            "image2.color_trim_materials",
            "image2.brand_vi_system",
            "image2.social_platform_finish",
            "image2.commercial_composition",
            "image2.campaign_background",
            "image2.poster_typography",
            "image2.low_quality_negative",
            "image2.brand_safety",
        ),
    ),
    "dreamina_chinese_marketing_default": MediaPromptRecipe(
        recipe_id="dreamina_chinese_marketing_default",
        channel="image_marketing_visual",
        target_engine="dreamina",
        display_name="Dreamina Chinese marketing visual default",
        module_ids=(
            "dreamina.chinese_social_poster",
            "dreamina.brand_asset_series",
            "dreamina.visual_finish",
            "dreamina.brand_guardrails",
            "dreamina.forbidden_elements",
        ),
    ),
}


def get_media_provider_prompt_compiler(
    target_engine: MediaPromptTarget,
) -> MediaProviderPromptCompiler:
    return MEDIA_PROVIDER_PROMPT_COMPILERS[target_engine]


def get_media_prompt_module(module_id: str) -> MediaPromptModule:
    return MEDIA_PROMPT_MODULE_LIBRARY[module_id]


def get_media_prompt_instruction_module(module_id: str) -> MediaPromptModule:
    return get_media_prompt_module(module_id)


def get_media_prompt_recipe(recipe_id: MediaPromptRecipeId) -> MediaPromptRecipe:
    return MEDIA_PROMPT_RECIPE_LIBRARY[recipe_id]


def list_media_prompt_recipes(
    *,
    channel: MediaPromptChannel | None = None,
    target_engine: MediaPromptTarget | None = None,
) -> tuple[MediaPromptRecipe, ...]:
    recipes = MEDIA_PROMPT_RECIPE_LIBRARY.values()
    if channel is not None:
        recipes = (recipe for recipe in recipes if recipe.channel == channel)
    if target_engine is not None:
        recipes = (recipe for recipe in recipes if recipe.target_engine == target_engine)
    return tuple(recipes)


def list_media_prompt_modules(
    *,
    channel: MediaPromptChannel | None = None,
    target_engine: MediaPromptTarget | None = None,
) -> tuple[MediaPromptModule, ...]:
    modules = MEDIA_PROMPT_MODULE_LIBRARY.values()
    if channel is not None:
        modules = (module for module in modules if module.channel == channel)
    if target_engine is not None:
        modules = (
            module for module in modules if target_engine in module.provider_targets
        )
    return tuple(modules)


def list_media_prompt_instruction_modules(
    *,
    channel: MediaPromptChannel | None = None,
    target_engine: MediaPromptTarget | None = None,
) -> tuple[MediaPromptModule, ...]:
    return list_media_prompt_modules(
        channel=channel,
        target_engine=target_engine,
    )


@dataclass(frozen=True, slots=True)
class ImagePromptSpec:
    business_brief: str
    model_prompt: str
    aspect_ratio: str = "landscape"
    style_notes: str = ""
    forbidden_elements: str = ""

    def to_prompt(self) -> str:
        return _join_prompt_sections(
            (
                ("业务说明", self.business_brief),
                ("模型 Prompt", self.model_prompt),
                ("画幅", self.aspect_ratio),
                ("风格要求", self.style_notes),
                ("禁用元素", self.forbidden_elements),
            )
        )


@dataclass(frozen=True, slots=True)
class VideoStoryboardSpec:
    business_brief: str
    model_prompt: str
    storyboard: str = ""
    voiceover: str = ""
    style_notes: str = ""
    forbidden_elements: str = ""

    def to_prompt(self) -> str:
        return _join_prompt_sections(
            (
                ("业务说明", self.business_brief),
                ("模型 Prompt", self.model_prompt),
                ("分镜", self.storyboard),
                ("旁白/字幕", self.voiceover),
                ("风格要求", self.style_notes),
                ("禁用元素", self.forbidden_elements),
            )
        )


@dataclass(frozen=True, slots=True)
class MediaGenerationRecord:
    record_id: str
    media_kind: MediaKind
    status: MediaGenerationStatus
    engine: str
    business_brief: str
    structured_prompt: str
    aspect_ratio: str = ""
    output_url: str = ""
    output_path: str = ""
    error_hint: str = ""
    created_at: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "record_id": self.record_id,
            "media_kind": self.media_kind,
            "status": self.status,
            "engine": self.engine,
            "business_brief": self.business_brief,
            "structured_prompt": self.structured_prompt,
            "aspect_ratio": self.aspect_ratio,
            "output_url": self.output_url,
            "output_path": self.output_path,
            "error_hint": self.error_hint,
            "created_at": self.created_at,
            "rollback_hint": self.rollback_hint,
        }

    @property
    def rollback_hint(self) -> str:
        target = self.output_path or self.output_url or self.record_id
        return f"回滚时停用本次生成记录 {self.record_id}，不要继续引用输出 {target}。"

    def to_card_detail(self) -> str:
        lines = [
            f"记录: `{self.record_id}`",
            f"状态: {self.status}",
            f"引擎: {self.engine}",
        ]
        if self.output_path:
            lines.append(f"输出文件: {self.output_path}")
        if self.output_url:
            lines.append(f"输出链接: {self.output_url}")
        if self.error_hint:
            lines.append(f"失败原因: {self.error_hint}")
        lines.append(self.rollback_hint)
        lines.append("")
        lines.append(self.structured_prompt)
        return "\n".join(lines)


def build_media_generation_record(
    *,
    media_kind: MediaKind,
    prompt: str,
    engine: str,
    status: MediaGenerationStatus,
    aspect_ratio: str = "",
    output_url: str = "",
    output_path: str = "",
    error_hint: str = "",
) -> MediaGenerationRecord:
    structured_prompt = build_structured_media_prompt(
        prompt,
        media_kind=media_kind,
        aspect_ratio=aspect_ratio or "landscape",
    )
    created_at = datetime.now(UTC).replace(microsecond=0).isoformat()
    seed = "|".join(
        (
            media_kind,
            engine,
            status,
            structured_prompt,
            output_url,
            output_path,
            error_hint,
            created_at,
        )
    )
    return MediaGenerationRecord(
        record_id=f"media_{sha256(seed.encode('utf-8')).hexdigest()[:12]}",
        media_kind=media_kind,
        status=status,
        engine=engine,
        business_brief=prompt.strip(),
        structured_prompt=structured_prompt,
        aspect_ratio=aspect_ratio,
        output_url=output_url,
        output_path=output_path,
        error_hint=error_hint,
        created_at=created_at,
    )


def build_structured_media_prompt(
    prompt: str,
    *,
    media_kind: MediaKind,
    aspect_ratio: str = "landscape",
    target_engine: MediaPromptTarget = "generic",
    brand_profile: MediaBrandVisualProfile | None = None,
    recipe_id: MediaPromptRecipeId | None = None,
    catalog_selection: PromptCatalogSelection | None = None,
) -> str:
    """Wrap raw employee text as a structured SOP media prompt.

    Existing callers may already provide a structured prompt. In that case keep it
    unchanged so reviewed prompts and tests remain stable.
    """
    cleaned = prompt.strip()
    if not cleaned:
        return cleaned
    if "业务说明" in cleaned and "模型 Prompt" in cleaned:
        return cleaned
    effective_recipe_id = recipe_id
    if catalog_selection is not None:
        cleaned = build_prompt_catalog_brief(
            cleaned,
            catalog_selection,
            language="en" if target_engine == "gpt-image-2" else "zh",
        )
        if effective_recipe_id is None and target_engine == "gpt-image-2":
            effective_recipe_id = get_prompt_catalog_preset(
                catalog_selection.preset_id
            ).target_recipe_id
    if media_kind == "image":
        compiler = get_media_provider_prompt_compiler(target_engine)
        if compiler.target_engine == "gpt-image-2":
            return build_gpt_image2_marketing_prompt(
                cleaned,
                aspect_ratio=aspect_ratio,
                brand_profile=brand_profile,
                recipe_id=effective_recipe_id,
            )
        if compiler.target_engine == "dreamina":
            return build_dreamina_marketing_prompt(
                cleaned,
                aspect_ratio=aspect_ratio,
                brand_profile=brand_profile,
                recipe_id=effective_recipe_id,
            )
        return ImagePromptSpec(
            business_brief=cleaned,
            model_prompt=cleaned,
            aspect_ratio=aspect_ratio,
            style_notes="遵守品牌口径；如缺少品牌素材，不要虚构具体 logo、权益或价格。",
            forbidden_elements="不得生成未经提供的客户承诺、价格优惠、官方背书或真实个人隐私。",
        ).to_prompt()
    return VideoStoryboardSpec(
        business_brief=cleaned,
        model_prompt=cleaned,
        storyboard="按 3-5 个镜头组织，镜头内容必须可执行；缺少事实时标注为创意假设。",
        voiceover="旁白需与画面一致，不编造产品数据、权益或客户承诺。",
        style_notes="节奏清晰，适配短视频平台；如无平台信息，按通用 16:9 版本处理。",
        forbidden_elements="不得生成未经提供的客户承诺、价格优惠、官方背书或真实个人隐私。",
    ).to_prompt()


def build_gpt_image2_marketing_prompt(
    prompt: str,
    *,
    aspect_ratio: str = "landscape",
    brand_profile: MediaBrandVisualProfile | None = None,
    recipe_id: MediaPromptRecipeId | None = None,
) -> str:
    """Compile a Chinese marketing brief into an Image 2-oriented visual prompt."""
    cleaned = prompt.strip()
    recipe = _resolve_media_prompt_recipe(
        recipe_id,
        default_recipe_id="image2_premium_key_visual",
        target_engine="gpt-image-2",
    )
    module_ids = recipe.module_ids
    return _join_prompt_sections(
        (
            ("业务说明", cleaned),
            ("Provider", "GPT Image 2 primary"),
            ("Prompt library", MEDIA_PROMPT_LIBRARY_VERSION),
            ("Prompt recipe", recipe.recipe_id),
            ("Prompt modules", ", ".join(module_ids)),
            ("Source brief, keep verbatim", cleaned),
            *_brand_profile_sections(brand_profile, language="en"),
            *_prompt_module_sections(module_ids),
            ("Aspect ratio", aspect_ratio),
        )
    )


def build_dreamina_marketing_prompt(
    prompt: str,
    *,
    aspect_ratio: str = "landscape",
    brand_profile: MediaBrandVisualProfile | None = None,
    recipe_id: MediaPromptRecipeId | None = None,
) -> str:
    """Compile an image brief for Dreamina's Chinese marketing visual strengths."""
    cleaned = prompt.strip()
    recipe = _resolve_media_prompt_recipe(
        recipe_id,
        default_recipe_id="dreamina_chinese_marketing_default",
        target_engine="dreamina",
    )
    module_ids = recipe.module_ids
    return _join_prompt_sections(
        (
            ("业务说明", cleaned),
            ("Provider", "Dreamina fallback or explicit opt-in"),
            ("Prompt library", MEDIA_PROMPT_LIBRARY_VERSION),
            ("Prompt recipe", recipe.recipe_id),
            ("Prompt modules", ", ".join(module_ids)),
            ("模型 Prompt", cleaned),
            *_brand_profile_sections(brand_profile, language="zh"),
            ("画幅", aspect_ratio),
            *_prompt_module_sections(module_ids),
        )
    )


def _resolve_media_prompt_recipe(
    recipe_id: MediaPromptRecipeId | None,
    *,
    default_recipe_id: MediaPromptRecipeId,
    target_engine: MediaPromptTarget,
) -> MediaPromptRecipe:
    recipe = get_media_prompt_recipe(recipe_id or default_recipe_id)
    if recipe.target_engine != target_engine:
        msg = (
            f"Media prompt recipe {recipe.recipe_id} targets {recipe.target_engine}, "
            f"not {target_engine}."
        )
        raise ValueError(msg)
    return recipe


def _brand_profile_sections(
    brand_profile: MediaBrandVisualProfile | None,
    *,
    language: MediaPromptLanguage,
) -> tuple[tuple[str, str], ...]:
    if brand_profile is None or not brand_profile.has_content:
        return ()
    if language == "zh":
        return (("品牌视觉 Profile", _format_brand_profile_zh(brand_profile)),)
    return (("Brand visual profile", _format_brand_profile_en(brand_profile)),)


def _format_brand_profile_en(brand_profile: MediaBrandVisualProfile) -> str:
    lines = _brand_profile_lines(
        brand_profile,
        labels={
            "brand_name": "brand",
            "vehicle_series": "vehicle series",
            "model_name": "model",
            "colors": "approved colors",
            "trims": "approved trims",
            "vi_colors": "VI colors",
            "typography": "typography",
            "logo_usage": "logo usage",
            "visual_effects": "visual effects",
            "asset_references": "asset references",
            "missing_assets": "missing assets",
            "source_note": "source",
        },
    )
    lines.append(
        "Use this profile as source-grounded visual context; do not invent missing "
        "brand assets, vehicle details, logos, claims, or official approvals."
    )
    return " | ".join(lines)


def _format_brand_profile_zh(brand_profile: MediaBrandVisualProfile) -> str:
    lines = _brand_profile_lines(
        brand_profile,
        labels={
            "brand_name": "品牌",
            "vehicle_series": "车系",
            "model_name": "车型",
            "colors": "指定颜色",
            "trims": "指定款式",
            "vi_colors": "VI 色",
            "typography": "字体/排版",
            "logo_usage": "Logo 使用",
            "visual_effects": "视觉效果",
            "asset_references": "素材来源",
            "missing_assets": "缺失素材",
            "source_note": "来源说明",
        },
    )
    lines.append("以上为有来源的视觉上下文；缺失素材不得臆造官方 Logo、车型细节、权益或认证。")
    return " | ".join(lines)


def _brand_profile_lines(
    brand_profile: MediaBrandVisualProfile,
    *,
    labels: dict[str, str],
) -> list[str]:
    lines: list[str] = []
    scalar_fields = (
        "brand_name",
        "vehicle_series",
        "model_name",
        "typography",
        "logo_usage",
        "source_note",
    )
    tuple_fields = (
        "colors",
        "trims",
        "vi_colors",
        "visual_effects",
        "asset_references",
        "missing_assets",
    )
    for field_name in scalar_fields:
        value = getattr(brand_profile, field_name)
        if value:
            lines.append(f"{labels[field_name]}={value}")
    for field_name in tuple_fields:
        value = getattr(brand_profile, field_name)
        if value:
            lines.append(f"{labels[field_name]}={', '.join(value)}")
    return lines


def _prompt_module_sections(module_ids: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    return tuple(
        (
            get_media_prompt_module(module_id).label,
            get_media_prompt_module(module_id).content,
        )
        for module_id in module_ids
    )


def _join_prompt_sections(sections: tuple[tuple[str, str], ...]) -> str:
    return "\n".join(
        f"{label}: {value.strip()}"
        for label, value in sections
        if value and value.strip()
    )
