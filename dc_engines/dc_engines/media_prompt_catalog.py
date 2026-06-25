"""Composable prompt catalog for low-friction media generation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

PromptCatalogSlot = Literal[
    "subject",
    "scene",
    "camera",
    "lighting",
    "style",
    "brand",
    "quality",
    "negative",
]

PromptAtomLanguage = Literal["en", "zh"]
PromptCatalogPresetId = Literal[
    "automotive_wechat_cover",
    "automotive_xhs_cover",
    "automotive_festival_poster",
    "automotive_launch_key_visual",
]

PROMPT_CATALOG_VERSION = "media_prompt_catalog.automotive_marketing.v1"
PROMPT_CATALOG_PRODUCT_POLICY = (
    "user_reference_first",
    "low_friction_preset_first",
    "internal_proposal_after_feedback",
)


@dataclass(frozen=True, slots=True)
class PromptCatalogAtom:
    atom_id: str
    slot: PromptCatalogSlot
    label: str
    zh_prompt: str
    en_prompt: str
    tags: tuple[str, ...] = ()

    def prompt_for(self, language: PromptAtomLanguage) -> str:
        if language == "zh":
            return self.zh_prompt
        return self.en_prompt


@dataclass(frozen=True, slots=True)
class PromptCatalogPreset:
    preset_id: PromptCatalogPresetId
    label: str
    description: str
    target_recipe_id: str
    atom_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PromptCatalogSelection:
    preset_id: PromptCatalogPresetId
    extra_atom_ids: tuple[str, ...] = ()
    custom_brief: str = ""


PROMPT_CATALOG_ATOMS: dict[str, PromptCatalogAtom] = {
    "subject.hero_vehicle": PromptCatalogAtom(
        atom_id="subject.hero_vehicle",
        slot="subject",
        label="主推车型",
        zh_prompt="主角是一台清晰可辨的主推车型，车身比例准确，车辆是第一视觉焦点。",
        en_prompt=(
            "The hero subject is a clearly identifiable promoted vehicle with "
            "accurate proportions, acting as the first visual focus."
        ),
        tags=("vehicle", "hero"),
    ),
    "subject.family_lifestyle": PromptCatalogAtom(
        atom_id="subject.family_lifestyle",
        slot="subject",
        label="家庭生活场景",
        zh_prompt="加入自然家庭生活语境，人物只做辅助氛围，不抢车型主体。",
        en_prompt=(
            "Add a natural family lifestyle context; people support the mood "
            "without competing with the vehicle."
        ),
        tags=("lifestyle", "family"),
    ),
    "subject.product_detail": PromptCatalogAtom(
        atom_id="subject.product_detail",
        slot="subject",
        label="产品细节特写",
        zh_prompt="突出车灯、轮毂、内饰或关键产品细节，细节真实、质感干净。",
        en_prompt=(
            "Highlight headlights, wheels, interior, or key product details with "
            "clean, realistic material quality."
        ),
        tags=("detail", "vehicle"),
    ),
    "scene.city_premium": PromptCatalogAtom(
        atom_id="scene.city_premium",
        slot="scene",
        label="高级城市背景",
        zh_prompt="背景为高级现代城市环境，干净、有层次，不使用廉价素材感背景。",
        en_prompt=(
            "Use a premium modern city environment with clean depth and no cheap "
            "stock-background feeling."
        ),
        tags=("city", "premium"),
    ),
    "scene.showroom_clean": PromptCatalogAtom(
        atom_id="scene.showroom_clean",
        slot="scene",
        label="干净展厅",
        zh_prompt="背景为干净汽车展厅或品牌空间，突出车身反光和产品质感。",
        en_prompt=(
            "Use a clean automotive showroom or brand space that emphasizes body "
            "reflections and product quality."
        ),
        tags=("showroom", "brand"),
    ),
    "scene.summer_refreshing": PromptCatalogAtom(
        atom_id="scene.summer_refreshing",
        slot="scene",
        label="夏日清爽",
        zh_prompt="夏日元素要清爽高级，阳光、冰块、水汽、水果自然融入画面。",
        en_prompt=(
            "Make summer elements feel refreshing and premium; sunlight, ice, "
            "mist, water, and fruit integrate naturally."
        ),
        tags=("summer", "festival"),
    ),
    "scene.festival_warm": PromptCatalogAtom(
        atom_id="scene.festival_warm",
        slot="scene",
        label="节日温暖",
        zh_prompt="节日氛围温暖克制，保留商业海报质感，避免堆砌装饰。",
        en_prompt=(
            "Use a warm but restrained seasonal mood with commercial poster "
            "quality; avoid decorative clutter."
        ),
        tags=("festival", "warm"),
    ),
    "camera.mobile_cover": PromptCatalogAtom(
        atom_id="camera.mobile_cover",
        slot="camera",
        label="移动端封面构图",
        zh_prompt="按移动端封面构图，主体明确，标题安全区充足，小图也能看清。",
        en_prompt=(
            "Compose for a mobile cover: clear subject, enough title-safe area, "
            "and readable thumbnail impact."
        ),
        tags=("mobile", "cover"),
    ),
    "camera.hero_low_angle": PromptCatalogAtom(
        atom_id="camera.hero_low_angle",
        slot="camera",
        label="英雄低角度",
        zh_prompt="使用轻微低角度英雄视角，增强车型力量感，但不要夸张变形。",
        en_prompt=(
            "Use a slight low-angle hero view to strengthen vehicle presence "
            "without exaggerated distortion."
        ),
        tags=("hero", "camera"),
    ),
    "camera.clean_negative_space": PromptCatalogAtom(
        atom_id="camera.clean_negative_space",
        slot="camera",
        label="干净留白",
        zh_prompt="画面保留干净留白，适合放中文标题和活动信息。",
        en_prompt=(
            "Keep clean negative space suitable for Chinese headlines and campaign "
            "information."
        ),
        tags=("layout", "text"),
    ),
    "lighting.commercial_softbox": PromptCatalogAtom(
        atom_id="lighting.commercial_softbox",
        slot="lighting",
        label="商业柔光",
        zh_prompt="商业广告级柔光，车漆和玻璃反光受控，质感高级。",
        en_prompt=(
            "Use commercial advertising softbox lighting with controlled paint and "
            "glass reflections."
        ),
        tags=("commercial", "lighting"),
    ),
    "lighting.golden_hour": PromptCatalogAtom(
        atom_id="lighting.golden_hour",
        slot="lighting",
        label="黄金时刻",
        zh_prompt="黄金时刻暖光，画面温暖但不过曝，适合生活方式传播。",
        en_prompt=(
            "Use golden-hour warm light, warm but not overexposed, suitable for "
            "lifestyle marketing."
        ),
        tags=("warm", "lifestyle"),
    ),
    "lighting.fresh_daylight": PromptCatalogAtom(
        atom_id="lighting.fresh_daylight",
        slot="lighting",
        label="清爽日光",
        zh_prompt="清爽自然日光，适合夏季、节日和轻松社媒封面。",
        en_prompt=(
            "Use fresh natural daylight suitable for summer, seasonal campaigns, "
            "and light social covers."
        ),
        tags=("daylight", "summer"),
    ),
    "style.wechat_cover": PromptCatalogAtom(
        atom_id="style.wechat_cover",
        slot="style",
        label="公众号封面",
        zh_prompt="公众号封面风格，信息层级清楚，视觉稳重，避免过多小字。",
        en_prompt=(
            "WeChat article cover style with clear information hierarchy, stable "
            "visual tone, and minimal small text."
        ),
        tags=("wechat", "cover"),
    ),
    "style.xhs_cover": PromptCatalogAtom(
        atom_id="style.xhs_cover",
        slot="style",
        label="小红书封面",
        zh_prompt="小红书封面风格，第一眼吸引人，生活感强，移动端缩略图醒目。",
        en_prompt=(
            "Rednote cover style: instantly attractive, lifestyle-oriented, and "
            "strong as a mobile thumbnail."
        ),
        tags=("rednote", "xhs", "cover"),
    ),
    "style.launch_event": PromptCatalogAtom(
        atom_id="style.launch_event",
        slot="style",
        label="上市发布主视觉",
        zh_prompt="新车上市或活动发布主视觉，画面正式、有发布感和品牌权威感。",
        en_prompt=(
            "Launch-event key visual style with formal campaign authority and a "
            "clear product announcement feeling."
        ),
        tags=("launch", "key_visual"),
    ),
    "brand.vi_disciplined": PromptCatalogAtom(
        atom_id="brand.vi_disciplined",
        slot="brand",
        label="品牌 VI 克制",
        zh_prompt="品牌 VI 使用克制统一，颜色、字体、Logo 区域保持一致。",
        en_prompt=(
            "Keep brand VI disciplined and consistent across colors, typography, "
            "and logo zones."
        ),
        tags=("brand", "vi"),
    ),
    "brand.no_fake_logo": PromptCatalogAtom(
        atom_id="brand.no_fake_logo",
        slot="brand",
        label="不臆造 Logo",
        zh_prompt="没有官方素材时不要臆造 Logo、徽章、价格、权益或认证信息。",
        en_prompt=(
            "Do not invent logos, badges, prices, benefits, or certifications when "
            "official assets are missing."
        ),
        tags=("brand", "safety"),
    ),
    "quality.advertising_grade": PromptCatalogAtom(
        atom_id="quality.advertising_grade",
        slot="quality",
        label="广告级质感",
        zh_prompt="广告级成片质感，高清、干净、可直接用于社媒初稿。",
        en_prompt=(
            "Advertising-grade finish, high-resolution, clean, and usable as a "
            "social-media first draft."
        ),
        tags=("quality", "commercial"),
    ),
    "quality.real_materials": PromptCatalogAtom(
        atom_id="quality.real_materials",
        slot="quality",
        label="真实材质",
        zh_prompt="车漆、玻璃、金属、冰块、水汽和阳光都要真实可信。",
        en_prompt=(
            "Make paint, glass, metal, ice, mist, and sunlight physically credible."
        ),
        tags=("quality", "materials"),
    ),
    "negative.no_low_end_poster": PromptCatalogAtom(
        atom_id="negative.no_low_end_poster",
        slot="negative",
        label="排除廉价海报",
        zh_prompt="排除廉价海报感、贴纸感太阳、粗糙渐变、杂乱背景和塑料质感。",
        en_prompt=(
            "Avoid low-end poster design, sticker-like sun elements, rough "
            "gradients, cluttered backgrounds, and plastic rendering."
        ),
        tags=("negative", "quality"),
    ),
    "negative.no_vehicle_distortion": PromptCatalogAtom(
        atom_id="negative.no_vehicle_distortion",
        slot="negative",
        label="排除车身崩坏",
        zh_prompt="排除车身比例错误、轮毂变形、车灯错位、玻璃反光混乱。",
        en_prompt=(
            "Avoid wrong body proportions, warped wheels, misplaced headlights, "
            "and chaotic glass reflections."
        ),
        tags=("negative", "vehicle"),
    ),
    "negative.no_text_noise": PromptCatalogAtom(
        atom_id="negative.no_text_noise",
        slot="negative",
        label="排除文字噪音",
        zh_prompt="排除乱码中文、错误品牌名、过多小字、伪造法律文字。",
        en_prompt=(
            "Avoid garbled Chinese text, wrong brand names, excessive small text, "
            "and fabricated legal copy."
        ),
        tags=("negative", "text"),
    ),
}

PROMPT_CATALOG_PRESETS: dict[PromptCatalogPresetId, PromptCatalogPreset] = {
    "automotive_wechat_cover": PromptCatalogPreset(
        preset_id="automotive_wechat_cover",
        label="公众号封面",
        description="稳重、清楚、适合文章首图和活动通知。",
        target_recipe_id="image2_social_cover",
        atom_ids=(
            "subject.hero_vehicle",
            "scene.city_premium",
            "camera.mobile_cover",
            "camera.clean_negative_space",
            "lighting.commercial_softbox",
            "style.wechat_cover",
            "brand.vi_disciplined",
            "brand.no_fake_logo",
            "quality.advertising_grade",
            "negative.no_low_end_poster",
            "negative.no_text_noise",
        ),
    ),
    "automotive_xhs_cover": PromptCatalogPreset(
        preset_id="automotive_xhs_cover",
        label="小红书封面",
        description="移动端吸睛、生活方式强、适合种草初稿。",
        target_recipe_id="image2_social_cover",
        atom_ids=(
            "subject.hero_vehicle",
            "subject.family_lifestyle",
            "scene.summer_refreshing",
            "camera.mobile_cover",
            "lighting.golden_hour",
            "style.xhs_cover",
            "brand.vi_disciplined",
            "quality.advertising_grade",
            "quality.real_materials",
            "negative.no_low_end_poster",
            "negative.no_vehicle_distortion",
        ),
    ),
    "automotive_festival_poster": PromptCatalogPreset(
        preset_id="automotive_festival_poster",
        label="节日海报",
        description="节日氛围明确，但不过度堆砌装饰。",
        target_recipe_id="image2_festival_campaign",
        atom_ids=(
            "subject.hero_vehicle",
            "scene.festival_warm",
            "scene.summer_refreshing",
            "camera.clean_negative_space",
            "lighting.fresh_daylight",
            "brand.vi_disciplined",
            "brand.no_fake_logo",
            "quality.advertising_grade",
            "quality.real_materials",
            "negative.no_low_end_poster",
            "negative.no_text_noise",
        ),
    ),
    "automotive_launch_key_visual": PromptCatalogPreset(
        preset_id="automotive_launch_key_visual",
        label="上市发布主视觉",
        description="正式发布感、车型权威感、适合活动主视觉。",
        target_recipe_id="image2_premium_key_visual",
        atom_ids=(
            "subject.hero_vehicle",
            "subject.product_detail",
            "scene.showroom_clean",
            "camera.hero_low_angle",
            "camera.clean_negative_space",
            "lighting.commercial_softbox",
            "style.launch_event",
            "brand.vi_disciplined",
            "brand.no_fake_logo",
            "quality.advertising_grade",
            "negative.no_vehicle_distortion",
            "negative.no_text_noise",
        ),
    ),
}


def get_prompt_catalog_atom(atom_id: str) -> PromptCatalogAtom:
    return PROMPT_CATALOG_ATOMS[atom_id]


def list_prompt_catalog_atoms(
    *,
    slot: PromptCatalogSlot | None = None,
    tag: str | None = None,
) -> tuple[PromptCatalogAtom, ...]:
    atoms = PROMPT_CATALOG_ATOMS.values()
    if slot is not None:
        atoms = (atom for atom in atoms if atom.slot == slot)
    if tag is not None:
        atoms = (atom for atom in atoms if tag in atom.tags)
    return tuple(atoms)


def get_prompt_catalog_preset(
    preset_id: PromptCatalogPresetId,
) -> PromptCatalogPreset:
    return PROMPT_CATALOG_PRESETS[preset_id]


def list_prompt_catalog_presets() -> tuple[PromptCatalogPreset, ...]:
    return tuple(PROMPT_CATALOG_PRESETS.values())


def build_prompt_catalog_context(
    selection: PromptCatalogSelection,
    *,
    language: PromptAtomLanguage = "en",
) -> str:
    preset = get_prompt_catalog_preset(selection.preset_id)
    atom_ids = _dedupe_atom_ids((*preset.atom_ids, *selection.extra_atom_ids))
    atoms = tuple(get_prompt_catalog_atom(atom_id) for atom_id in atom_ids)
    slot_lines = _format_atoms_by_slot(atoms, language=language)
    if selection.custom_brief:
        slot_lines.append(f"custom brief: {selection.custom_brief.strip()}")
    return "\n".join(
        (
            f"Prompt catalog preset: {preset.preset_id} ({preset.label})",
            f"Target prompt recipe: {preset.target_recipe_id}",
            "Selected prompt atoms:",
            *slot_lines,
        )
    )


def build_prompt_catalog_brief(
    raw_brief: str,
    selection: PromptCatalogSelection,
    *,
    language: PromptAtomLanguage = "en",
) -> str:
    cleaned = raw_brief.strip()
    catalog_context = build_prompt_catalog_context(selection, language=language)
    if not cleaned:
        return catalog_context
    return f"{cleaned}\n\n{catalog_context}"


def _format_atoms_by_slot(
    atoms: tuple[PromptCatalogAtom, ...],
    *,
    language: PromptAtomLanguage,
) -> list[str]:
    grouped: dict[PromptCatalogSlot, list[str]] = {}
    for atom in atoms:
        grouped.setdefault(atom.slot, []).append(atom.prompt_for(language))
    return [f"- {slot}: {'; '.join(prompts)}" for slot, prompts in grouped.items()]


def _dedupe_atom_ids(atom_ids: tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    deduped: list[str] = []
    for atom_id in atom_ids:
        if atom_id not in seen:
            seen.add(atom_id)
            deduped.append(atom_id)
    return tuple(deduped)
