"""Shared SOP prompt helpers for image and video generation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Literal

MediaKind = Literal["image", "video", "image2video"]
MediaGenerationStatus = Literal["running", "succeeded", "failed"]


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
    if media_kind == "image":
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


def _join_prompt_sections(sections: tuple[tuple[str, str], ...]) -> str:
    return "\n".join(
        f"{label}: {value.strip()}"
        for label, value in sections
        if value and value.strip()
    )
