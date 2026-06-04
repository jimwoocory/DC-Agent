"""Encode and decode Obsidian governance notes."""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

import yaml

from .models import GovernedMemory

SECTION_CANONICAL = "Canonical Memory"
SECTION_EVIDENCE = "Evidence"
SECTION_HUMAN_NOTES = "Human Notes"
SECTION_PROMOTION_PREVIEW = "Promotion Preview"

REQUIRED_FRONTMATTER = {
    "memory_id",
    "source_system",
    "source_id",
    "source_path",
    "source_hash",
    "title",
    "memory_kind",
    "review_status",
    "confidence",
    "sensitivity",
    "owner",
    "project_id",
    "governance_version",
    "reviewer",
    "review_reason",
    "merged_into",
    "tags",
    "links",
}


def render_governance_note(memory: GovernedMemory) -> str:
    """Render a governed memory as an editable Obsidian Markdown note."""

    frontmatter = {
        "memory_id": memory.memory_id,
        "source_system": memory.source_system,
        "source_id": memory.source_id,
        "source_path": memory.source_path,
        "source_hash": memory.source_hash,
        "title": memory.title,
        "memory_kind": memory.memory_kind,
        "review_status": memory.review_status,
        "confidence": memory.confidence,
        "sensitivity": memory.sensitivity,
        "owner": memory.owner,
        "project_id": memory.project_id,
        "governance_version": memory.governance_version,
        "reviewer": memory.approved_by,
        "review_reason": "",
        "merged_into": "",
        "tags": memory.tags,
        "links": memory.links,
    }
    yaml_text = yaml.safe_dump(
        frontmatter,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    ).strip()
    evidence = _render_evidence(memory)
    promotion_preview = _render_promotion_preview(memory)
    return (
        f"---\n{yaml_text}\n---\n\n"
        f"# {memory.title}\n\n"
        f"## {SECTION_CANONICAL}\n\n"
        f"{memory.canonical_text.strip()}\n\n"
        f"## {SECTION_EVIDENCE}\n\n"
        f"{evidence}\n\n"
        f"## {SECTION_HUMAN_NOTES}\n\n"
        "Reviewer notes go here.\n\n"
        f"## {SECTION_PROMOTION_PREVIEW}\n\n"
        f"{promotion_preview}\n"
    )


def parse_governance_note(markdown: str, *, now: str = "") -> GovernedMemory:
    """Parse an Obsidian governance note into a governed memory."""

    frontmatter, body = _split_frontmatter(markdown)
    missing = sorted(REQUIRED_FRONTMATTER - set(frontmatter))
    if missing:
        raise ValueError(f"missing governance frontmatter: {', '.join(missing)}")

    canonical_text = _extract_section(body, SECTION_CANONICAL)
    if not canonical_text:
        raise ValueError("Canonical Memory section is required")

    approved_by = ""
    approved_at = ""
    reviewer = str(frontmatter.get("reviewer") or "").strip()
    review_status = str(frontmatter["review_status"])
    if review_status == "approved":
        approved_by = reviewer
        approved_at = now

    return GovernedMemory(
        memory_id=str(frontmatter["memory_id"]),
        source_system=str(frontmatter["source_system"]),  # type: ignore[arg-type]
        source_id=str(frontmatter["source_id"]),
        source_path=str(frontmatter.get("source_path") or ""),
        source_hash=str(frontmatter["source_hash"]),
        title=str(frontmatter["title"]),
        summary=_extract_summary(body),
        canonical_text=canonical_text,
        memory_kind=str(frontmatter["memory_kind"]),  # type: ignore[arg-type]
        review_status=review_status,  # type: ignore[arg-type]
        confidence=float(frontmatter["confidence"]),
        sensitivity=str(frontmatter["sensitivity"]),  # type: ignore[arg-type]
        owner=str(frontmatter.get("owner") or ""),
        project_id=str(frontmatter.get("project_id") or ""),
        tags=_string_list(frontmatter.get("tags")),
        links=_string_list(frontmatter.get("links")),
        obsidian_note_path="",
        governance_version=int(frontmatter["governance_version"]),
        created_at="",
        updated_at=now,
        approved_at=approved_at,
        approved_by=approved_by,
    )


def apply_note_path(memory: GovernedMemory, note_path: str) -> GovernedMemory:
    """Return a copy with the Obsidian note path attached."""

    return replace(memory, obsidian_note_path=note_path)


def _split_frontmatter(markdown: str) -> tuple[dict[str, Any], str]:
    if not markdown.startswith("---\n"):
        raise ValueError("governance note must start with YAML frontmatter")
    try:
        _, yaml_text, body = markdown.split("---", 2)
    except ValueError as exc:
        raise ValueError("governance note frontmatter is not closed") from exc
    parsed = yaml.safe_load(yaml_text) or {}
    if not isinstance(parsed, dict):
        raise ValueError("governance note frontmatter must be a mapping")
    return parsed, body.lstrip()


def _extract_section(body: str, heading: str) -> str:
    pattern = re.compile(
        rf"^##\s+{re.escape(heading)}\s*$\n(?P<content>.*?)(?=^##\s+|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(body)
    if not match:
        return ""
    return match.group("content").strip()


def _extract_summary(body: str) -> str:
    evidence = _extract_section(body, SECTION_EVIDENCE)
    for line in evidence.splitlines():
        line = line.strip().lstrip("-").strip()
        if line.startswith("Extracted summary:"):
            return line.removeprefix("Extracted summary:").strip()
    canonical = _extract_section(body, SECTION_CANONICAL)
    return canonical[:500]


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)]


def _render_evidence(memory: GovernedMemory) -> str:
    lines = [
        f"- Source: {memory.source_path}",
        f"- Source hash: {memory.source_hash}",
        f"- Source system: {memory.source_system}",
        f"- Source ID: {memory.source_id}",
        f"- Extracted summary: {memory.summary}",
    ]
    if memory.links:
        lines.append(f"- Links: {', '.join(memory.links)}")
    return "\n".join(lines)


def _render_promotion_preview(memory: GovernedMemory) -> str:
    if memory.review_status == "approved":
        runtime_status = "eligible for approved-only runtime recall"
    elif memory.review_status == "sensitive_blocked":
        runtime_status = (
            "blocked from runtime recall because sensitivity review blocked it"
        )
    else:
        runtime_status = (
            f"blocked until review_status is approved; current={memory.review_status}"
        )
    return "\n".join(
        [
            f"- Runtime recall: {runtime_status}",
            f"- Citation: {memory.source_path or memory.source_id}",
            f"- Sensitivity: {memory.sensitivity}",
        ]
    )
