#!/usr/bin/env python3
"""Install versioned bundled skills into the runtime data/skills directory."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = REPO_ROOT / "bundled" / "skills"
DEFAULT_TARGET = REPO_ROOT / "data" / "skills"

APPROVED_AUTHORING_SKILLS = frozenset(
    {
        "obsidian-markdown",
        "obsidian-bases",
        "json-canvas",
    }
)
DEFERRED_EXECUTION_SKILLS = frozenset({"obsidian-cli", "defuddle"})


class SyncConfigError(ValueError):
    """Invalid bundled skill sync configuration."""


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _validate_sync_paths(
    source: Path, target: Path, *, allow_custom_target: bool
) -> None:
    allowed_target = DEFAULT_TARGET.resolve()
    if not allow_custom_target and target != allowed_target:
        raise SyncConfigError(
            f"target must be the runtime skills root: {allowed_target}"
        )
    if (
        source == target
        or _is_relative_to(source, target)
        or _is_relative_to(target, source)
    ):
        raise SyncConfigError("source and target must not be the same or nested")


def _skill_dirs(source: Path) -> list[Path]:
    if not source.is_dir():
        return []
    return sorted(
        path
        for path in source.iterdir()
        if path.is_dir() and (path / "SKILL.md").is_file()
    )


def sync_bundled_skills(
    *,
    source: Path = DEFAULT_SOURCE,
    target: Path = DEFAULT_TARGET,
    dry_run: bool = False,
    allow_custom_target: bool = False,
) -> list[str]:
    """Sync bundled skill directories into the runtime skills root.

    Only directories present under ``source`` are managed. Other runtime skills
    in ``target`` are left untouched.
    """
    source = source.resolve()
    target = target.resolve()
    _validate_sync_paths(source, target, allow_custom_target=allow_custom_target)

    installed: list[str] = []
    if not dry_run:
        for skill_name in DEFERRED_EXECUTION_SKILLS:
            stale = target / skill_name
            if stale.exists():
                shutil.rmtree(stale)

    for skill_dir in _skill_dirs(source):
        skill_name = skill_dir.name
        if skill_name not in APPROVED_AUTHORING_SKILLS:
            continue
        destination = target / skill_name
        installed.append(skill_name)
        if dry_run:
            continue
        target.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(skill_dir, destination)
    return installed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    installed = sync_bundled_skills(
        source=args.source,
        target=args.target,
        dry_run=args.dry_run,
    )
    prefix = "Would sync" if args.dry_run else "Synced"
    print(f"{prefix} {len(installed)} bundled skill(s): {', '.join(installed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
