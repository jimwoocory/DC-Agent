from __future__ import annotations

import json
from pathlib import Path

import pytest
from dc_engines.obsidian_vault_automation import (
    ObsidianVaultAutomation,
    VaultAccessError,
    VaultAutomationConfig,
)


def _make_wrapper(
    vault_root: Path,
    *,
    audit_log_path: Path | None = None,
) -> ObsidianVaultAutomation:
    return ObsidianVaultAutomation(
        VaultAutomationConfig(
            vault_roots=(vault_root,),
            audit_log_path=audit_log_path,
            actor="test-agent",
        )
    )


def _seed_vault(vault_root: Path) -> Path:
    notes_dir = vault_root / "Notes"
    notes_dir.mkdir(parents=True)
    note_path = notes_dir / "Launch.md"
    note_path.write_text(
        "\n".join(
            [
                "---",
                "title: Launch SOP",
                "tags:",
                "  - launch",
                "review_status: approved",
                "---",
                "",
                "# Launch SOP",
                "",
                "Launch work requires customer approval.",
            ]
        ),
        encoding="utf-8",
    )
    (vault_root / "Notes" / "Other.md").write_text(
        "# Other\n\nNo launch details here.",
        encoding="utf-8",
    )
    return note_path


def test_wrapper_enforces_vault_root_allowlist(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    _seed_vault(vault_root)
    outside_note = tmp_path / "outside.md"
    outside_note.write_text("secret", encoding="utf-8")
    wrapper = _make_wrapper(vault_root)

    assert wrapper.read("Notes/Launch.md").startswith("---")
    with pytest.raises(VaultAccessError, match="outside configured vault roots"):
        wrapper.read(outside_note)

    denied = wrapper.audit_records[-1]
    assert denied.operation == "read"
    assert denied.allowed is False


def test_wrapper_blocks_traversal_and_symlink_escape(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    _seed_vault(vault_root)
    outside_note = tmp_path / "outside.md"
    outside_note.write_text("secret", encoding="utf-8")
    (vault_root / "escape.md").symlink_to(outside_note)
    wrapper = _make_wrapper(vault_root)

    with pytest.raises(VaultAccessError, match="outside configured vault roots"):
        wrapper.read("../outside.md")
    with pytest.raises(VaultAccessError, match="outside configured vault roots"):
        wrapper.read("escape.md")
    with pytest.raises(VaultAccessError, match="outside configured vault roots"):
        wrapper.search("secret")
    with pytest.raises(VaultAccessError, match="outside configured vault roots"):
        wrapper.list("")

    denied_paths = [
        record.path for record in wrapper.audit_records if not record.allowed
    ]
    assert "../outside.md" in denied_paths
    assert "escape.md" in denied_paths


def test_wrapper_supports_read_only_vault_operations(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    note_path = _seed_vault(vault_root)
    wrapper = _make_wrapper(vault_root)

    entries = wrapper.list("Notes")
    content = wrapper.read("Notes/Launch.md")
    hits = wrapper.search("customer approval")
    metadata = wrapper.metadata(note_path)
    frontmatter = wrapper.frontmatter("Notes/Launch.md")

    assert {entry.path for entry in entries} == {
        "Notes/Launch.md",
        "Notes/Other.md",
    }
    assert "Launch work requires customer approval." in content
    assert [(hit.path, hit.line_number) for hit in hits] == [("Notes/Launch.md", 10)]
    assert metadata["path"] == "Notes/Launch.md"
    assert metadata["kind"] == "file"
    assert len(metadata["sha256"]) == 64
    assert frontmatter["title"] == "Launch SOP"
    assert frontmatter["tags"] == ["launch"]
    assert {record.operation for record in wrapper.audit_records} >= {
        "list",
        "read",
        "search",
        "metadata",
        "frontmatter",
    }


def test_wrapper_denies_write_delete_and_shell_execution(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    note_path = _seed_vault(vault_root)
    wrapper = _make_wrapper(vault_root)

    plan = wrapper.plan_write("Notes/Launch.md", "changed")
    with pytest.raises(VaultAccessError, match="write is not enabled"):
        wrapper.write("Notes/Launch.md", "changed")
    with pytest.raises(VaultAccessError, match="delete is not enabled"):
        wrapper.delete("Notes/Launch.md")
    with pytest.raises(VaultAccessError, match="shell execution is forbidden"):
        wrapper.run_shell("obsidian-cli export")

    assert plan["dry_run"] is True
    assert note_path.read_text(encoding="utf-8").count("Launch SOP") == 2
    denied_operations = [
        record.operation for record in wrapper.audit_records if not record.allowed
    ]
    assert denied_operations == ["write", "delete", "shell"]
    dry_run_records = [record for record in wrapper.audit_records if record.dry_run]
    assert [record.operation for record in dry_run_records] == ["plan_write"]


def test_wrapper_writes_append_only_audit_records(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    _seed_vault(vault_root)
    audit_log_path = tmp_path / "audit" / "obsidian-vault.jsonl"
    wrapper = _make_wrapper(vault_root, audit_log_path=audit_log_path)

    wrapper.read("Notes/Launch.md", actor="reader")
    wrapper.plan_write("Notes/New.md", "draft", actor="planner")

    rows = [
        json.loads(line)
        for line in audit_log_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [row["operation"] for row in rows] == ["read", "plan_write"]
    assert rows[0]["actor"] == "reader"
    assert rows[0]["allowed"] is True
    assert rows[0]["dry_run"] is False
    assert rows[1]["actor"] == "planner"
    assert rows[1]["dry_run"] is True
    for row in rows:
        assert {
            "timestamp",
            "actor",
            "operation",
            "path",
            "allowed",
            "dry_run",
            "reason",
            "payload",
        } <= row.keys()
