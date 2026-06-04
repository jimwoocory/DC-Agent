#!/usr/bin/env python3
"""Ensure local test scripts are connected to an official verification entry."""

from __future__ import annotations

import argparse
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

SCRIPT_SUFFIXES = {
    ".py",
    ".js",
    ".mjs",
    ".cjs",
    ".ts",
    ".tsx",
    ".sh",
    ".ps1",
    ".yaml",
    ".yml",
}

EXCLUDED_PREFIXES = (
    ".git/",
    ".venv/",
    ".uv-cache/",
    ".antigravitycli/",
    ".claude/",
    ".codex/",
    ".obsidian/",
    ".playwright-cli/",
    "cache/",
    "conversions/",
    "outputs/",
    "hermes-agent/",
    "hermes-config/",
    "hermes-webui/",
    "hermes-webui-state/",
)

EXTERNAL_PREFIXES = ("data/skills/",)

REFERENCED_EXTERNAL_PREFIXES = ("openclaw-control-center/",)

NON_SCRIPT_PREFIXES = (
    "data/attachments/",
    "data/router_bench/",
    "data/watchdog/",
    "docs/grey_test_video/",
)


@dataclass(frozen=True)
class Classification:
    path: str
    status: str
    entrypoint: str


def git_lines(args: list[str]) -> list[str]:
    result = subprocess.run(
        ["git", *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return [line.rstrip() for line in result.stdout.splitlines() if line.strip()]


def tracked_paths() -> set[str]:
    return set(git_lines(["ls-files"]))


def visible_status_paths() -> set[str]:
    paths: set[str] = set()
    for line in git_lines(["status", "--porcelain", "--untracked-files=all"]):
        status = line[:2]
        if "D" in status:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        paths.add(path)
    return paths


def looks_like_test_path(path: str) -> bool:
    name = Path(path).name
    stem = Path(path).stem
    return name.startswith("test") or stem.endswith("test") or ".test." in name


def is_excluded(path: str) -> bool:
    parts = set(Path(path).parts)
    if "node_modules" in parts or "__pycache__" in parts:
        return True
    return path.startswith(EXCLUDED_PREFIXES)


def classify_path(path: str) -> Classification | None:
    if is_excluded(path) or not looks_like_test_path(path):
        return None

    suffix = Path(path).suffix
    if path.startswith(NON_SCRIPT_PREFIXES) or suffix not in SCRIPT_SUFFIXES:
        return Classification(path, "non_script", "not executable test code")

    if path.startswith(REFERENCED_EXTERNAL_PREFIXES):
        return Classification(
            path,
            "referenced_external",
            "referenced by DC-Agent runtime entry but outside the main gate",
        )

    if path.startswith(EXTERNAL_PREFIXES):
        return Classification(path, "external", "outside the main DC-Agent gate")

    if path.startswith("tests/"):
        return Classification(path, "covered", "scripts/run_pytests_ci.sh ./tests")

    if path.startswith("dc_engines/tests/"):
        return Classification(
            path, "covered", "scripts/agent-check.sh --profile targeted"
        )

    if path == "data/plugins/llm_router/test_dc_router_path.py":
        return Classification(
            path, "covered", "scripts/agent-check.sh --profile targeted"
        )

    if path.startswith("dashboard/tests/") and path.endswith(".test.mjs"):
        return Classification(
            path, "covered", "dashboard package test + dashboard_ci.yml"
        )

    if path.startswith("docs/tests/"):
        return Classification(path, "covered", ".github/workflows/sync-wiki.yml")

    if path.startswith(".github/workflows/"):
        return Classification(path, "covered", "GitHub Actions workflow")

    return Classification(path, "unclosed", "no official verification entry found")


def all_candidate_paths() -> list[str]:
    paths = tracked_paths() | visible_status_paths() | filesystem_test_paths()
    return sorted(path for path in paths if not path.startswith(os.sep))


def filesystem_test_paths() -> set[str]:
    root = Path.cwd()
    paths: set[str] = set()
    for current, dirnames, filenames in os.walk(root):
        rel_dir = Path(current).relative_to(root).as_posix()
        if rel_dir == ".":
            rel_dir = ""

        kept_dirs: list[str] = []
        for dirname in dirnames:
            rel_path = f"{rel_dir}/{dirname}/" if rel_dir else f"{dirname}/"
            if dirname in {"node_modules", "__pycache__"} or rel_path.startswith(
                EXCLUDED_PREFIXES
            ):
                continue
            kept_dirs.append(dirname)
        dirnames[:] = kept_dirs

        for filename in filenames:
            path = f"{rel_dir}/{filename}" if rel_dir else filename
            if looks_like_test_path(path):
                paths.add(path)
    return paths


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--strict-external",
        action="store_true",
        help="treat external project test scripts as failures",
    )
    parser.add_argument(
        "--report-external",
        action="store_true",
        help="list external test scripts that are outside the main DC-Agent gate",
    )
    args = parser.parse_args()

    classifications = [
        item for path in all_candidate_paths() if (item := classify_path(path))
    ]
    unclosed = [item for item in classifications if item.status == "unclosed"]
    external = [
        item
        for item in classifications
        if item.status in {"external", "referenced_external"}
    ]
    referenced_external = [
        item for item in classifications if item.status == "referenced_external"
    ]

    if args.strict_external:
        unclosed.extend(external)

    if unclosed:
        print("Test closure check failed; these test scripts lack a formal entry:")
        for item in unclosed[:80]:
            print(f"- {item.path} ({item.entrypoint})")
        if len(unclosed) > 80:
            print(f"... and {len(unclosed) - 80} more")
        return 1

    covered_count = sum(1 for item in classifications if item.status == "covered")
    non_script_count = sum(1 for item in classifications if item.status == "non_script")
    print(
        "Test closure check passed: "
        f"{covered_count} covered, {non_script_count} non-script, "
        f"{len(external)} external "
        f"({len(referenced_external)} referenced by runtime entry)."
    )
    if referenced_external and args.report_external:
        print("Referenced external test scripts are not closed by the main gate:")
        for item in referenced_external[:20]:
            print(f"- {item.path}")
        if len(referenced_external) > 20:
            print(f"... and {len(referenced_external) - 20} more")
    if external and args.report_external:
        print("External test scripts are outside the main DC-Agent gate:")
        plain_external = [item for item in external if item.status == "external"]
        for item in plain_external[:20]:
            print(f"- {item.path}")
        if len(plain_external) > 20:
            print(f"... and {len(plain_external) - 20} more")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
