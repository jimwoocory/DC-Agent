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
    max_plan_bytes: int = 256 * 1024,
    plan_ttl_seconds: int = 3600,
    write_execution_enabled: bool = False,
    write_approval_token: str | None = None,
) -> ObsidianVaultAutomation:
    return ObsidianVaultAutomation(
        VaultAutomationConfig(
            vault_roots=(vault_root,),
            audit_log_path=audit_log_path,
            actor="test-agent",
            max_plan_bytes=max_plan_bytes,
            plan_ttl_seconds=plan_ttl_seconds,
            write_execution_enabled=write_execution_enabled,
            write_approval_token=write_approval_token,
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


def test_wrapper_creates_audited_write_plan_without_modifying_vault(
    tmp_path: Path,
) -> None:
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    note_path = _seed_vault(vault_root)
    original_content = note_path.read_text(encoding="utf-8")
    planned_content = original_content.replace(
        "customer approval",
        "legal and customer approval",
    )
    wrapper = _make_wrapper(vault_root)

    plan = wrapper.plan_write("Notes/Launch.md", planned_content)

    assert plan["dry_run"] is True
    assert plan["plan_id"].startswith("ova-plan-")
    assert plan["action"] == "update"
    assert plan["risk"] == "medium"
    assert plan["existing_sha256"]
    assert plan["content_sha256"] != plan["existing_sha256"]
    assert plan["created_at"] < plan["expires_at"]
    assert "--- a/Notes/Launch.md" in plan["diff"]
    assert "+++ b/Notes/Launch.md" in plan["diff"]
    assert "+Launch work requires legal and customer approval." in plan["diff"]
    assert note_path.read_text(encoding="utf-8") == original_content
    audit_record = wrapper.audit_records[-1]
    assert audit_record.operation == "plan_write"
    assert audit_record.allowed is True
    assert audit_record.dry_run is True
    assert audit_record.payload["plan_id"] == plan["plan_id"]
    assert audit_record.payload["diff"] == plan["diff"]


def test_wrapper_plans_new_file_creation_without_creating_file(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    _seed_vault(vault_root)
    wrapper = _make_wrapper(vault_root)

    plan = wrapper.plan_write("Notes/New.md", "# New\n")

    assert plan["action"] == "create"
    assert plan["risk"] == "low"
    assert plan["existing_sha256"] is None
    assert "+# New" in plan["diff"]
    assert not (vault_root / "Notes" / "New.md").exists()


def test_wrapper_rejects_write_plan_over_size_limit(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    _seed_vault(vault_root)
    wrapper = _make_wrapper(vault_root, max_plan_bytes=4)

    with pytest.raises(VaultAccessError, match="exceeds byte limit"):
        wrapper.plan_write("Notes/New.md", "too large")

    denied = wrapper.audit_records[-1]
    assert denied.operation == "plan_write"
    assert denied.allowed is False
    assert denied.dry_run is True
    assert denied.payload["max_plan_bytes"] == 4


def test_wrapper_denies_write_plan_execution_by_default(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    _seed_vault(vault_root)
    wrapper = _make_wrapper(vault_root)
    content = "# New\n"
    plan = wrapper.plan_write("Notes/New.md", content)

    with pytest.raises(VaultAccessError, match="write execution is disabled"):
        wrapper.execute_write_plan(plan, content, approval_token="token")

    assert not (vault_root / "Notes" / "New.md").exists()
    denied = wrapper.audit_records[-1]
    assert denied.operation == "execute_write_plan"
    assert denied.allowed is False


def test_wrapper_executes_approved_write_plan(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    note_path = _seed_vault(vault_root)
    original_content = note_path.read_text(encoding="utf-8")
    content = original_content.replace("customer approval", "signed approval")
    wrapper = _make_wrapper(
        vault_root,
        write_execution_enabled=True,
        write_approval_token="approved-token",
    )
    plan = wrapper.plan_write("Notes/Launch.md", content)

    result = wrapper.execute_write_plan(
        plan,
        content,
        approval_token="approved-token",
    )

    assert result["executed"] is True
    assert result["plan_id"] == plan["plan_id"]
    assert result["action"] == "update"
    assert "signed approval" in note_path.read_text(encoding="utf-8")
    executed = wrapper.audit_records[-1]
    assert executed.operation == "execute_write_plan"
    assert executed.allowed is True
    assert executed.dry_run is False
    assert executed.payload["content_sha256"] == plan["content_sha256"]


def test_wrapper_rejects_write_plan_execution_with_invalid_token(
    tmp_path: Path,
) -> None:
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    _seed_vault(vault_root)
    wrapper = _make_wrapper(
        vault_root,
        write_execution_enabled=True,
        write_approval_token="approved-token",
    )
    content = "# New\n"
    plan = wrapper.plan_write("Notes/New.md", content)

    with pytest.raises(VaultAccessError, match="approval token is invalid"):
        wrapper.execute_write_plan(plan, content, approval_token="wrong-token")

    assert not (vault_root / "Notes" / "New.md").exists()


def test_wrapper_rejects_write_plan_execution_after_file_drift(
    tmp_path: Path,
) -> None:
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    note_path = _seed_vault(vault_root)
    original_content = note_path.read_text(encoding="utf-8")
    content = original_content.replace("customer approval", "signed approval")
    wrapper = _make_wrapper(
        vault_root,
        write_execution_enabled=True,
        write_approval_token="approved-token",
    )
    plan = wrapper.plan_write("Notes/Launch.md", content)
    note_path.write_text(original_content + "\nDrift\n", encoding="utf-8")

    with pytest.raises(VaultAccessError, match="changed after write plan"):
        wrapper.execute_write_plan(
            plan,
            content,
            approval_token="approved-token",
        )

    assert "signed approval" not in note_path.read_text(encoding="utf-8")


def test_wrapper_rejects_write_plan_execution_with_content_hash_mismatch(
    tmp_path: Path,
) -> None:
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    _seed_vault(vault_root)
    wrapper = _make_wrapper(
        vault_root,
        write_execution_enabled=True,
        write_approval_token="approved-token",
    )
    plan = wrapper.plan_write("Notes/New.md", "# Planned\n")

    with pytest.raises(VaultAccessError, match="content hash does not match"):
        wrapper.execute_write_plan(
            plan,
            "# Different\n",
            approval_token="approved-token",
        )

    assert not (vault_root / "Notes" / "New.md").exists()


def test_wrapper_rejects_expired_write_plan_execution(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    _seed_vault(vault_root)
    wrapper = _make_wrapper(
        vault_root,
        plan_ttl_seconds=-1,
        write_execution_enabled=True,
        write_approval_token="approved-token",
    )
    content = "# New\n"
    plan = wrapper.plan_write("Notes/New.md", content)

    with pytest.raises(VaultAccessError, match="Write plan has expired"):
        wrapper.execute_write_plan(
            plan,
            content,
            approval_token="approved-token",
        )

    assert not (vault_root / "Notes" / "New.md").exists()


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
