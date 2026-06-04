from __future__ import annotations

from dataclasses import dataclass, field

from .contracts import HarnessTaskCreateRequest

HARNESS_GUARDRAIL_VERSION = "2026-05-21"

HARNESS_TRUTH_GUARD = """

## DC-Agent 真实性铁律（必须遵守）
无论是闲聊、写作、图片/视频任务、邮箱、日常问答还是深度分析，都不能编造事实。
- 不得凭空声称公司、客户、员工、项目、预算、数据、文件内容、工具执行结果或 Harness/Hermes 状态。
- 没有来源时要明确说「我无法确认」或「需要补充真实材料」，并说明需要什么材料。
- 可以做创意或模板，但必须标明是「示例/假设/待替换」，不能包装成真实情况。
- 引用用户提供的原文、附件摘要、知识库或 Harness task 时，要区分事实、推断和待确认项。

## 小助手语气要求
表达要温柔、耐心、体贴，像一位成熟可靠的行政/运营助理在照顾员工和老板的工作体验。
- 真实性约束要坚定，但不要用审问、训斥、冷冰冰的风控语气。
- 需要材料时，用「我先帮你把这件事稳住」「为了不让内容失真」「我还需要确认几项」这类关怀式表达。
- 可以适度亲切，但不要过度撒娇、油腻或表演化；专业、轻柔、让人愿意继续补资料。
- 对老板要简洁稳重，对员工要多一点鼓励和减压。
"""

_BOSS_TERMS = ("老板", "老总", "老大", "杨总", "总经理", "董事长", "CEO")
_COLLEAGUE_TERMS = ("同事", "员工", "负责人", "部门", "协作", "配合")
_PRIVATE_TERMS = ("私聊", "单独", "个人", "保密", "先别发群里", "只跟你说")
_SOURCE_TERMS = ("客户", "项目", "预算", "数据", "文件", "文档", "合同", "知识库")


@dataclass(slots=True)
class HarnessGuardrailAssessment:
    policy_version: str = HARNESS_GUARDRAIL_VERSION
    required_rules: list[str] = field(default_factory=list)
    risk_flags: list[str] = field(default_factory=list)
    routing_hints: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "policy_version": self.policy_version,
            "required_rules": list(dict.fromkeys(self.required_rules)),
            "risk_flags": list(dict.fromkeys(self.risk_flags)),
            "routing_hints": list(dict.fromkeys(self.routing_hints)),
            "evidence": list(dict.fromkeys(self.evidence))[:8],
        }


def assess_harness_guardrails(
    request: HarnessTaskCreateRequest,
) -> HarnessGuardrailAssessment:
    text = _request_text(request)
    assessment = HarnessGuardrailAssessment(
        required_rules=[
            "truthfulness_required",
            "source_attribution_required",
        ],
        risk_flags=[],
        routing_hints=[],
        evidence=[],
    )

    if _contains_any(text, _BOSS_TERMS):
        assessment.required_rules.extend(
            [
                "boss_formal_address_required",
                "boss_answer_conclusion_first",
            ]
        )
        assessment.risk_flags.append("boss_context")
        assessment.routing_hints.append("boss_skill_candidate")
        assessment.evidence.append("matched boss terms")

    if _contains_any(text, _COLLEAGUE_TERMS):
        assessment.required_rules.append("colleague_persona_boundary_required")
        assessment.risk_flags.append("colleague_context")
        assessment.routing_hints.append("colleague_skill_candidate")
        assessment.evidence.append("matched colleague terms")

    if _contains_any(text, _PRIVATE_TERMS):
        assessment.required_rules.extend(
            [
                "private_chat_scope_required",
                "do_not_promote_private_content_without_confirmation",
            ]
        )
        assessment.risk_flags.append("private_context")
        assessment.evidence.append("matched private-scope terms")

    if _contains_any(text, _SOURCE_TERMS):
        assessment.required_rules.append("business_fact_source_required")
        assessment.risk_flags.append("business_fact_context")
        assessment.evidence.append("matched business-source terms")

    return assessment


def _request_text(request: HarnessTaskCreateRequest) -> str:
    payload = request.payload or {}
    parts = [
        request.title,
        request.domain,
        str(payload.get("brief") or ""),
        str(payload.get("message_text") or ""),
    ]
    return "\n".join(part for part in parts if part).lower()


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term.lower() in text for term in terms)
