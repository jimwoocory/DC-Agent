from __future__ import annotations

import json
from pathlib import Path

import pytest
from dc_engines.obsidian_vault import (
    ObsidianVault,
    VaultAccessError,
    VaultSecurityPolicy,
    parse_frontmatter,
)


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "Vault"
    (vault / "RawRefs").mkdir(parents=True)
    (vault / "Review").mkdir()
    (vault / "RawRefs" / "source.md").write_text(
        "---\n"
        "title: Source Note\n"
        "tags:\n"
        "  - rawref\n"
        "---\n\n"
        "# Source Note\n\n"
        "Alpha customer rollout plan.\n",
        encoding="utf-8",
    )
    (vault / "Review" / "workbench.md").write_text(
        "# Review Workbench\n\nLinks to [[Source Note]] and [[RawRefs/source]].\n",
        encoding="utf-8",
    )
    return vault


def test_rejects_path_traversal_and_symlink_escape(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    (vault / "outside-link.md").symlink_to(outside)

    wrapper = ObsidianVault(VaultSecurityPolicy(vault_root=vault))

    with pytest.raises(VaultAccessError, match="relative"):
        wrapper.read_note(outside)

    with pytest.raises(VaultAccessError, match="escapes"):
        wrapper.read_note("../outside.md")

    with pytest.raises(VaultAccessError, match="escapes"):
        wrapper.read_note("outside-link.md")


def test_read_only_wrapper_lists_reads_searches_frontmatter_and_backlinks(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    wrapper = ObsidianVault(VaultSecurityPolicy(vault_root=vault))

    entries = wrapper.list_directory(".")
    assert [entry.path for entry in entries] == ["RawRefs", "Review"]

    note = wrapper.read_note("RawRefs/source.md")
    assert note.path == "RawRefs/source.md"
    assert note.frontmatter["title"] == "Source Note"
    assert note.frontmatter["tags"] == ["rawref"]
    assert "Alpha customer rollout plan." in note.content

    frontmatter, body = parse_frontmatter(note.content)
    assert frontmatter["title"] == "Source Note"
    assert body.startswith("# Source Note")

    hits = wrapper.search_notes("customer rollout")
    assert [(hit.path, hit.line_number) for hit in hits] == [("RawRefs/source.md", 9)]

    backlinks = wrapper.read_backlinks("RawRefs/source.md")
    assert [(hit.path, hit.line_number) for hit in backlinks] == [
        ("Review/workbench.md", 3)
    ]


def test_write_requests_are_dry_run_audited_and_do_not_mutate_vault(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    audit_log = tmp_path / "audit" / "obsidian_vault.jsonl"
    wrapper = ObsidianVault(
        VaultSecurityPolicy(
            vault_root=vault,
            audit_log_path=audit_log,
            allowed_write_paths=("Review",),
        )
    )

    plan = wrapper.create_note(
        "Review/generated.md",
        "# Generated\n",
        actor="test-agent",
        dry_run=True,
    )

    assert plan.operation == "create_note"
    assert plan.target_path == "Review/generated.md"
    assert plan.dry_run is True
    assert plan.allowed is False
    assert not (vault / "Review" / "generated.md").exists()

    audit_event = json.loads(audit_log.read_text(encoding="utf-8").strip())
    assert audit_event["actor"] == "test-agent"
    assert audit_event["action"] == "create_note"
    assert audit_event["target_path"] == "Review/generated.md"
    assert audit_event["dry_run"] is True
    assert audit_event["allowed"] is False
    assert audit_event["result"] == "dry_run"

    with pytest.raises(VaultAccessError, match="does not execute writes"):
        wrapper.create_note("Review/generated.md", "# Generated\n", dry_run=False)

    assert not (vault / "Review" / "generated.md").exists()
