"""Content SOP metadata helpers for business routing."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class ContentDepartment(str, Enum):
    CLIENT_DEPT = "client_dept"
    PLANNING = "planning"
    UNKNOWN = "unknown"


class ContentType(str, Enum):
    COPY = "copy"
    IMAGE = "image"
    VIDEO = "video"
    CAMPAIGN = "campaign"
    MIXED = "mixed"
    UNKNOWN = "unknown"


class ContentMaterialStatus(str, Enum):
    READY = "ready"
    PARTIAL = "partial"
    NEEDS_MATERIALS = "needs_materials"


class ContentRiskLevel(str, Enum):
    NORMAL = "normal"
    FACT_SENSITIVE = "fact_sensitive"
    CLIENT_COMMITMENT = "client_commitment"
    BRAND_SENSITIVE = "brand_sensitive"


@dataclass(frozen=True, slots=True)
class ContentSopMetadata:
    department: ContentDepartment
    content_type: ContentType
    material_status: ContentMaterialStatus
    risk_level: ContentRiskLevel

    @property
    def is_content_sop(self) -> bool:
        if self.content_type is ContentType.UNKNOWN:
            return False
        if (
            self.content_type is ContentType.CAMPAIGN
            and self.department is ContentDepartment.UNKNOWN
        ):
            return False
        return True

    def to_metadata(self) -> dict[str, str]:
        if not self.is_content_sop:
            return {}
        return {
            "content_sop": "true",
            "department": self.department.value,
            "content_type": self.content_type.value,
            "material_status": self.material_status.value,
            "risk_level": self.risk_level.value,
        }


_CLIENT_RE = re.compile(
    r"(客户|客户部|邀约|回访|续约|私域|话术|触达|vip|渠道|线索|权益|报价|承诺)",
    re.IGNORECASE,
)
_PLANNING_RE = re.compile(
    r"(策划|策划部|策略|规划|分镜|短视频|脚本|传播|洞察|brief|storyboard)",
    re.IGNORECASE,
)
_COPY_RE = re.compile(r"(文案|话术|标题|slogan|广告语|邮件|短信|正文)", re.IGNORECASE)
_IMAGE_RE = re.compile(r"(生图|配图|海报|图片|画面|视觉|prompt|提示词)", re.IGNORECASE)
_VIDEO_RE = re.compile(r"(视频|短视频|分镜|脚本|旁白|镜头|剪辑)", re.IGNORECASE)
_CAMPAIGN_RE = re.compile(r"(方案|活动|campaign|营销|传播|推广|全案)", re.IGNORECASE)
_MATERIAL_SIGNAL_RE = re.compile(
    r"(https?://|feishu\.cn|<attachment_summary>|<feishu_doc|\[image\]|\[file\]|\[video\]|附件|链接|文档|资料|素材|截图)",
    re.IGNORECASE,
)
_CONCRETE_SIGNAL_RE = re.compile(
    r"(品牌|产品|车型|客户|项目|活动|时间|预算|目标|受众|人群|渠道|平台|卖点|权益|KPI|周期)",
    re.IGNORECASE,
)


def infer_content_sop_metadata(
    text: str,
    *,
    attachment_summary: str | None = None,
    has_attachments: bool = False,
) -> ContentSopMetadata:
    combined = f"{text}\n{attachment_summary or ''}"
    department = _infer_department(combined)
    content_type = _infer_content_type(combined)
    return ContentSopMetadata(
        department=department,
        content_type=content_type,
        material_status=_infer_material_status(
            combined,
            attachment_summary=attachment_summary,
            has_attachments=has_attachments,
            is_content_sop=content_type is not ContentType.UNKNOWN,
        ),
        risk_level=_infer_risk_level(combined, department),
    )


def _infer_department(text: str) -> ContentDepartment:
    client_score = len(_CLIENT_RE.findall(text))
    planning_score = len(_PLANNING_RE.findall(text))
    if client_score > planning_score:
        return ContentDepartment.CLIENT_DEPT
    if planning_score > 0:
        return ContentDepartment.PLANNING
    if client_score > 0:
        return ContentDepartment.CLIENT_DEPT
    return ContentDepartment.UNKNOWN


def _infer_content_type(text: str) -> ContentType:
    hits: list[ContentType] = []
    if _COPY_RE.search(text):
        hits.append(ContentType.COPY)
    if _IMAGE_RE.search(text):
        hits.append(ContentType.IMAGE)
    if _VIDEO_RE.search(text):
        hits.append(ContentType.VIDEO)
    if _CAMPAIGN_RE.search(text):
        hits.append(ContentType.CAMPAIGN)
    unique_hits = tuple(dict.fromkeys(hits))
    if len(unique_hits) >= 2:
        return ContentType.MIXED
    if unique_hits:
        return unique_hits[0]
    return ContentType.UNKNOWN


def _infer_material_status(
    text: str,
    *,
    attachment_summary: str | None,
    has_attachments: bool,
    is_content_sop: bool,
) -> ContentMaterialStatus:
    if not is_content_sop:
        return ContentMaterialStatus.NEEDS_MATERIALS
    if attachment_summary and _CONCRETE_SIGNAL_RE.search(text):
        return ContentMaterialStatus.READY
    if has_attachments or _MATERIAL_SIGNAL_RE.search(text):
        return ContentMaterialStatus.PARTIAL
    if len(set(_CONCRETE_SIGNAL_RE.findall(text))) >= 3:
        return ContentMaterialStatus.READY
    return ContentMaterialStatus.NEEDS_MATERIALS


def _infer_risk_level(
    text: str,
    department: ContentDepartment,
) -> ContentRiskLevel:
    if re.search(r"(报价|价格|优惠|权益|承诺|合同|续约|客户身份)", text, re.IGNORECASE):
        return ContentRiskLevel.CLIENT_COMMITMENT
    if re.search(r"(品牌口径|禁用词|合规|危机|舆情|官方)", text, re.IGNORECASE):
        return ContentRiskLevel.BRAND_SENSITIVE
    if department is ContentDepartment.CLIENT_DEPT:
        return ContentRiskLevel.FACT_SENSITIVE
    return ContentRiskLevel.NORMAL
