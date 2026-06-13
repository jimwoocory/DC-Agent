from pathlib import Path

import pytest

from scripts.sync_bundled_skills import SyncConfigError, sync_bundled_skills


def _write_skill(root: Path, name: str) -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name}\n---\n# {name}\n",
        encoding="utf-8",
    )
    return skill_dir


def test_sync_bundled_skills_installs_authoring_skills_only(tmp_path: Path) -> None:
    source = tmp_path / "bundled" / "skills"
    target = tmp_path / "data" / "skills"
    _write_skill(source, "obsidian-markdown")
    _write_skill(source, "obsidian-cli")
    _write_skill(source, "defuddle")
    _write_skill(source, "unknown-bundled-skill")

    installed = sync_bundled_skills(
        source=source,
        target=target,
        allow_custom_target=True,
    )

    assert installed == ["obsidian-markdown"]
    assert (target / "obsidian-markdown" / "SKILL.md").is_file()
    assert not (target / "obsidian-cli").exists()
    assert not (target / "defuddle").exists()
    assert not (target / "unknown-bundled-skill").exists()


def test_sync_bundled_skills_removes_stale_deferred_runtime_skills(
    tmp_path: Path,
) -> None:
    source = tmp_path / "bundled" / "skills"
    target = tmp_path / "data" / "skills"
    _write_skill(source, "obsidian-markdown")
    _write_skill(target, "obsidian-cli")
    _write_skill(target, "defuddle")
    custom = _write_skill(target, "custom-runtime-skill")

    sync_bundled_skills(source=source, target=target, allow_custom_target=True)

    assert not (target / "obsidian-cli").exists()
    assert not (target / "defuddle").exists()
    assert (custom / "SKILL.md").is_file()


def test_sync_bundled_skills_preserves_unmanaged_runtime_skills(
    tmp_path: Path,
) -> None:
    source = tmp_path / "bundled" / "skills"
    target = tmp_path / "data" / "skills"
    _write_skill(source, "obsidian-markdown")
    custom = _write_skill(target, "custom-runtime-skill")

    sync_bundled_skills(source=source, target=target, allow_custom_target=True)

    assert (target / "obsidian-markdown" / "SKILL.md").is_file()
    assert (custom / "SKILL.md").is_file()


def test_sync_bundled_skills_dry_run_does_not_write(tmp_path: Path) -> None:
    source = tmp_path / "bundled" / "skills"
    target = tmp_path / "data" / "skills"
    _write_skill(source, "json-canvas")

    installed = sync_bundled_skills(
        source=source,
        target=target,
        dry_run=True,
        allow_custom_target=True,
    )

    assert installed == ["json-canvas"]
    assert not target.exists()


def test_sync_bundled_skills_rejects_nested_source_and_target(
    tmp_path: Path,
) -> None:
    source = tmp_path / "bundled" / "skills"
    _write_skill(source, "obsidian-markdown")

    with pytest.raises(SyncConfigError):
        sync_bundled_skills(
            source=source,
            target=source,
            allow_custom_target=True,
        )


def test_sync_bundled_skills_rejects_custom_target_by_default(
    tmp_path: Path,
) -> None:
    source = tmp_path / "bundled" / "skills"
    target = tmp_path / "data" / "skills"
    _write_skill(source, "obsidian-markdown")

    with pytest.raises(SyncConfigError):
        sync_bundled_skills(source=source, target=target)

    with pytest.raises(SyncConfigError):
        sync_bundled_skills(
            source=source,
            target=source / "runtime",
            allow_custom_target=True,
        )
