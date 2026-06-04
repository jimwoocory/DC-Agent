from __future__ import annotations

import re

from .contracts import MaterialIntakeAssessment, RequiredInput, Scenario

_FEISHU_URL_RE = re.compile(r"https?://[^\s]*?feishu\.cn/(wiki|docx|docs)/", re.I)
_URL_RE = re.compile(r"https?://", re.I)
_ATTACHMENT_MARKERS = (
    "<attachment_summary>",
    "<feishu_doc",
    "[image]",
    "[file]",
    "[audio]",
    "[video]",
    "截图",
    "附件",
    "链接",
    "文档",
    "原文",
    "会议记录",
    "聊天记录",
)


def assess_material_intake(
    scenario: Scenario,
    message_text: str,
) -> MaterialIntakeAssessment:
    """Conservatively assess whether a department workflow has enough material.

    This is intentionally heuristic. It does not pretend to extract exact fields;
    it gives Harness and later UI layers a stable intake state to close the loop.
    """
    text = message_text.strip()
    normalized = "".join(text.lower().split())
    signals = _provided_signals(text)
    missing: list[RequiredInput] = []

    for item in scenario.required_inputs:
        if not item.required:
            continue
        if not _input_is_mentioned(item, normalized):
            missing.append(item)

    if not missing:
        return MaterialIntakeAssessment(status="ready", provided_signals=signals)
    if signals:
        return MaterialIntakeAssessment(
            status="partial",
            provided_signals=signals,
            missing_required_inputs=tuple(missing),
        )
    return MaterialIntakeAssessment(
        status="needs_materials",
        provided_signals=signals,
        missing_required_inputs=tuple(missing),
    )


def _provided_signals(text: str) -> tuple[str, ...]:
    signals: list[str] = []
    if _FEISHU_URL_RE.search(text):
        signals.append("feishu_doc_url")
    elif _URL_RE.search(text):
        signals.append("external_url")
    for marker in _ATTACHMENT_MARKERS:
        if marker in text and marker not in {"链接", "文档"}:
            signals.append(f"marker:{marker}")
    return tuple(dict.fromkeys(signals))


def _input_is_mentioned(item: RequiredInput, normalized_text: str) -> bool:
    candidates = (item.key, item.label, *item.examples)
    return any(_normalize(candidate) in normalized_text for candidate in candidates)


def _normalize(value: str) -> str:
    return "".join(str(value or "").lower().split())
