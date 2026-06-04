#!/usr/bin/env python3
"""Check that generated cache, backup, and runtime output files stay out of git."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys

FORBIDDEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "python cache/build artifact",
        re.compile(
            r"(^|/)(__pycache__|\.pytest_cache|\.ruff_cache)(/|$)"
            r"|\.py[cod]$|\$py\.class$|\.so$"
        ),
    ),
    (
        "backup file",
        re.compile(
            r"(^|/)_backup_[^/]+|(^|/)[^/]*_backup_[^/]+|\.py\.bak$|\.bak($|\.)"
        ),
    ),
    (
        "runtime output directory",
        re.compile(
            r"(^|/)(tmp|output|logs)(/|$)|^data/(temp|output|knowledge_base)(/|$)"
        ),
    ),
)

EXCLUDED_PREFIXES = (
    ".git/",
    ".venv/",
    ".uv-cache/",
    "hermes-agent/",
    "hermes-config/",
    "hermes-webui/",
    "hermes-webui-state/",
)


def git_lines(args: list[str]) -> list[str]:
    result = subprocess.run(
        ["git", *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def staged_paths() -> list[str]:
    return git_lines(["diff", "--cached", "--name-only", "--diff-filter=ACMR"])


def tracked_paths() -> list[str]:
    return git_lines(["ls-files"])


def visible_status_paths() -> list[str]:
    paths: list[str] = []
    for line in git_lines(["status", "--porcelain", "--untracked-files=all"]):
        status = line[:2]
        if "D" in status:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        paths.append(path)
    return paths


def is_excluded(path: str) -> bool:
    return path.startswith(EXCLUDED_PREFIXES)


def violation_for(path: str) -> str | None:
    if is_excluded(path):
        return None
    for label, pattern in FORBIDDEN_PATTERNS:
        if pattern.search(path):
            return label
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--staged",
        action="store_true",
        help="check only staged paths, for pre-commit hooks",
    )
    args = parser.parse_args()

    paths = staged_paths() if args.staged else tracked_paths() + visible_status_paths()
    violations = sorted(
        {(path, reason) for path in paths if (reason := violation_for(path))}
    )

    if not violations:
        print("Repository hygiene check passed.")
        return 0

    print(
        "Repository hygiene check failed; remove or ignore these files:",
        file=sys.stderr,
    )
    for path, reason in violations[:80]:
        print(f"- {path} ({reason})", file=sys.stderr)
    if len(violations) > 80:
        print(f"... and {len(violations) - 80} more", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
