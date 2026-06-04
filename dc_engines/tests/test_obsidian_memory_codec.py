from __future__ import annotations

import pytest
from dc_engines.memory_governance.models import GovernedMemory
from dc_engines.memory_governance.obsidian_codec import (
    apply_note_path,
    parse_governance_note,
    render_governance_note,
)


def sample_memory() -> GovernedMemory:
    return GovernedMemory(
        memory_id="mem_nas_001",
        source_system="nas",
        source_id="nas:doc-1",
        source_path="/Users/dianchi/nas_kb/projects/doc-1.md",
        source_hash="sha256:abc",
        title="Customer A delivery SOP",
        summary="Customer A delivery takes four weeks.",
        canonical_text="Customer A delivery takes four weeks.",
        memory_kind="process",
        review_status="need_review",
        confidence=0.74,
        sensitivity="internal",
        owner="谭媛尹",
        project_id="customer-a",
        tags=["dc-agent-memory", "delivery"],
        links=["[[Customer A delivery SOP]]"],
        obsidian_note_path="",
        governance_version=1,
        created_at="2026-06-04T00:00:00Z",
        updated_at="2026-06-04T00:00:00Z",
        approved_at="",
        approved_by="",
    )


def test_render_governance_note_includes_required_frontmatter() -> None:
    markdown = render_governance_note(sample_memory())

    assert markdown.startswith("---\n")
    assert "memory_id: mem_nas_001" in markdown
    assert "source_path: /Users/dianchi/nas_kb/projects/doc-1.md" in markdown
    assert "source_hash: sha256:abc" in markdown
    assert "review_status: need_review" in markdown
    assert "sensitivity: internal" in markdown
    assert "governance_version: 1" in markdown
    assert "## Canonical Memory" in markdown
    assert "## Promotion Preview" in markdown


def test_parse_governance_note_round_trips_memory_identity() -> None:
    markdown = render_governance_note(sample_memory())

    parsed = parse_governance_note(markdown, now="2026-06-04T00:10:00Z")

    assert parsed.memory_id == "mem_nas_001"
    assert parsed.source_system == "nas"
    assert parsed.source_path == "/Users/dianchi/nas_kb/projects/doc-1.md"
    assert parsed.source_hash == "sha256:abc"
    assert parsed.canonical_text == "Customer A delivery takes four weeks."
    assert parsed.tags == ["dc-agent-memory", "delivery"]
    assert parsed.links == ["[[Customer A delivery SOP]]"]


def test_parse_governance_note_uses_edited_canonical_memory() -> None:
    markdown = render_governance_note(sample_memory()).replace(
        "Customer A delivery takes four weeks.",
        "Customer A delivery normally takes five weeks after approval.",
        1,
    )

    parsed = parse_governance_note(markdown, now="2026-06-04T00:10:00Z")

    assert (
        parsed.canonical_text
        == "Customer A delivery normally takes five weeks after approval."
    )


def test_parse_governance_note_requires_source_hash() -> None:
    markdown = render_governance_note(sample_memory()).replace(
        "source_hash: sha256:abc\n",
        "",
    )

    with pytest.raises(ValueError, match="source_hash"):
        parse_governance_note(markdown)


def test_parse_governance_note_sets_approval_metadata() -> None:
    markdown = (
        render_governance_note(sample_memory())
        .replace("review_status: need_review", "review_status: approved")
        .replace("reviewer: ''", "reviewer: dianchi")
    )

    parsed = parse_governance_note(markdown, now="2026-06-04T00:10:00Z")

    assert parsed.review_status == "approved"
    assert parsed.approved_by == "dianchi"
    assert parsed.approved_at == "2026-06-04T00:10:00Z"


def test_apply_note_path_returns_copy_with_obsidian_path() -> None:
    memory = sample_memory()

    updated = apply_note_path(
        memory,
        "ObsidianVault/40_MemoryGovernance/Inbox/mem_nas_001.md",
    )

    assert memory.obsidian_note_path == ""
    assert (
        updated.obsidian_note_path
        == "ObsidianVault/40_MemoryGovernance/Inbox/mem_nas_001.md"
    )
