from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

QualityGateStatus = Literal["passed", "blocked", "review_required"]

CONTENT_SOP_QUALITY_GATES: tuple[dict[str, str], ...] = (
    {
        "id": "materials",
        "label": "资料完整性",
        "description": "必填资料齐备，或已进入补资料流程。",
    },
    {
        "id": "sources",
        "label": "来源依据",
        "description": "涉及事实、项目、客户、权益或历史资料时必须有来源。",
    },
    {
        "id": "deliverables",
        "label": "交付物完整性",
        "description": "文案、生图 prompt、视频脚本/分镜和审查清单按场景齐备。",
    },
    {
        "id": "assumptions",
        "label": "创意假设边界",
        "description": "创意假设与来源事实分开，不把假设写成事实。",
    },
    {
        "id": "human_review",
        "label": "人工确认",
        "description": "客户承诺、价格权益、品牌口径和外发内容必须人工确认。",
    },
)


@dataclass(frozen=True, slots=True)
class ContentSopQualityResult:
    status: QualityGateStatus
    score: int
    blocked_reasons: tuple[str, ...] = ()
    review_reasons: tuple[str, ...] = ()

    @property
    def can_deliver(self) -> bool:
        return self.status == "passed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "score": self.score,
            "blocked_reasons": list(self.blocked_reasons),
            "review_reasons": list(self.review_reasons),
            "can_deliver": self.can_deliver,
        }


def build_content_sop_quality_policy() -> dict[str, Any]:
    return {
        "policy_version": "2026-06-04",
        "gates": [dict(item) for item in CONTENT_SOP_QUALITY_GATES],
        "minimum_score": 80,
        "blocking_rules": [
            "generation_allowed=false",
            "missing_required_inputs not empty",
            "required deliverable output missing",
            "fact-sensitive output without source_citations",
        ],
        "review_rules": [
            "client_commitment risk",
            "brand_sensitive risk",
            "external-use deliverable",
        ],
    }


def evaluate_content_sop_payload(
    payload: dict[str, Any],
    result: dict[str, Any] | None = None,
) -> ContentSopQualityResult:
    result = result or {}
    blocked: list[str] = []
    review: list[str] = []
    score = 100

    if payload.get("generation_allowed") is False:
        blocked.append("资料不足，禁止进入生成。")
        score -= 35
    if payload.get("missing_required_inputs"):
        blocked.append("仍有必填字段缺失。")
        score -= 20

    required_outputs = _expected_output_keys(payload)
    missing_outputs = [
        key
        for key in required_outputs
        if key in _core_output_keys() and not _has_value(result.get(key))
    ]
    if result and missing_outputs:
        blocked.append("交付物缺失: " + ", ".join(missing_outputs))
        score -= min(30, 10 * len(missing_outputs))

    source_citations = result.get("source_citations") or payload.get("source_citations")
    if _needs_source(payload) and not _has_value(source_citations):
        blocked.append("事实敏感内容缺少来源依据。")
        score -= 25

    channel_policy = payload.get("communication_channel_policy") or {}
    if (
        isinstance(channel_policy, dict)
        and channel_policy.get("should_use_email_format") is False
        and _result_looks_like_email(result)
    ):
        blocked.append("客户触达未明确要求邮件，禁止输出邮件格式。")
        score -= 25

    risk_level = str(payload.get("risk_level") or "")
    if risk_level in {"client_commitment", "brand_sensitive"}:
        review.append("风险等级要求人工确认。")
        score -= 5
    if payload.get("review_required_by_default") is True:
        review.append("内容 SOP 默认需要员工确认后外发。")

    score = max(0, min(100, score))
    if blocked:
        status: QualityGateStatus = "blocked"
    elif review:
        status = "review_required"
    else:
        status = "passed"
    return ContentSopQualityResult(
        status=status,
        score=score,
        blocked_reasons=tuple(blocked),
        review_reasons=tuple(review),
    )


def _expected_output_keys(payload: dict[str, Any]) -> list[str]:
    outputs = payload.get("expected_outputs") or []
    keys: list[str] = []
    for item in outputs:
        if isinstance(item, dict) and item.get("key"):
            keys.append(str(item["key"]))
    return keys


def _core_output_keys() -> set[str]:
    return {
        "message_draft",
        "image_prompt",
        "video_script",
        "storyboard",
        "source_citations",
        "review_checklist",
    }


def _needs_source(payload: dict[str, Any]) -> bool:
    if payload.get("source_citations"):
        return False
    truth_requirements = " ".join(
        str(item) for item in payload.get("truth_requirements") or []
    )
    return any(
        marker in truth_requirements
        for marker in ("不得编造", "必须来自", "来源", "客户", "权益", "价格", "品牌")
    )


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict, set)):
        return bool(value)
    return True


def _result_looks_like_email(result: dict[str, Any]) -> bool:
    if _has_value(result.get("email_subject")) or _has_value(result.get("email_body")):
        return True
    text = "\n".join(str(value) for value in result.values() if isinstance(value, str))
    return any(marker in text for marker in ("邮件主题", "邮件正文", "此致", "敬礼"))
